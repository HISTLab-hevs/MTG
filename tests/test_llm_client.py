"""Retry-with-rising-temperature behaviour of LLMClient.generate.

A reasoning model under guided decoding can occasionally diverge and never emit
a stop token; the per-attempt timeout interrupts it. Because temperature 0 is
deterministic, the retry must change sampling to escape the divergence, so each
retry nudges the temperature up by RETRY_TEMPERATURE_STEP. These tests pin that
contract without touching the network.
"""

import httpx
import pytest
from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

from app.llm.client import RETRY_TEMPERATURE_STEP, LLMClient

# The conftest autouse fixture stubs LLMClient.generate for every test; here we
# are testing generate itself, so restore the real method (captured at import,
# before any stubbing) for this module.
_REAL_GENERATE = LLMClient.generate


@pytest.fixture(autouse=True)
def _real_generate(monkeypatch):
    monkeypatch.setattr(LLMClient, "generate", _REAL_GENERATE)


class _StubCompletions:
    """Records the temperature of each call and replays a scripted outcome."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.temperatures = []

    def create(self, *, model, temperature, messages, extra_body):
        self.temperatures.append(temperature)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Msg:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})


def _reply(text):
    return type("R", (), {"choices": [_Msg(text)]})


@pytest.fixture
def patched_openai(monkeypatch):
    """Patch OpenAI so .chat.completions is our stub; return a setter."""
    holder = {}

    def make(outcomes):
        stub = _StubCompletions(outcomes)
        client = type("C", (), {"chat": type("Ch", (), {"completions": stub})})
        monkeypatch.setattr("app.llm.client.OpenAI", lambda **kw: client)
        holder["stub"] = stub
        return stub

    holder["make"] = make
    return holder


def _client(max_retries=2):
    return LLMClient(model="m", base_url="http://x/v1", max_retries=max_retries)


def test_succeeds_first_try_keeps_temperature(patched_openai):
    stub = patched_openai["make"]([_reply("  ok  ")])
    out = _client().generate("hi", temperature=0.0)
    assert out == "ok"  # stripped
    assert stub.temperatures == [0.0]


def test_timeout_retries_with_higher_temperature(patched_openai):
    timeout = APITimeoutError(request=None)
    stub = patched_openai["make"]([timeout, _reply("recovered")])
    out = _client().generate("hi", temperature=0.0)
    assert out == "recovered"
    # First attempt at the asked temperature, retry one step warmer.
    assert stub.temperatures == [0.0, RETRY_TEMPERATURE_STEP]


def test_exhausts_retries_then_raises(patched_openai):
    timeout = APITimeoutError(request=None)
    stub = patched_openai["make"]([timeout, timeout, timeout])
    with pytest.raises(APITimeoutError):
        _client(max_retries=2).generate("hi", temperature=0.2)
    # max_retries=2 means three attempts, each warmer than the last.
    assert stub.temperatures == [0.2, 0.2 + RETRY_TEMPERATURE_STEP, 0.2 + 2 * RETRY_TEMPERATURE_STEP]


def test_connection_error_is_retried_not_just_timeout(patched_openai):
    """A dropped/refused connection (endpoint restarting, network blip) is a
    transient failure, so it must be retried like a timeout — not propagated on
    the first attempt."""
    conn = APIConnectionError(request=httpx.Request("POST", "http://x/v1"))
    stub = patched_openai["make"]([conn, _reply("recovered")])
    out = _client().generate("hi", temperature=0.0)
    assert out == "recovered"
    assert stub.temperatures == [0.0, RETRY_TEMPERATURE_STEP]


def test_rate_limit_is_retried(patched_openai):
    """A 429 under concurrency is transient (the server is batching) and is
    retried rather than surfaced as an error."""
    resp = httpx.Response(429, request=httpx.Request("POST", "http://x/v1"))
    rate = RateLimitError("slow down", response=resp, body=None)
    stub = patched_openai["make"]([rate, _reply("ok")])
    assert _client().generate("hi") == "ok"
    assert len(stub.temperatures) == 2  # retried once


def test_server_error_is_retried(patched_openai):
    """A 500 from a model server under load clears on a second attempt."""
    resp = httpx.Response(500, request=httpx.Request("POST", "http://x/v1"))
    boom = InternalServerError("boom", response=resp, body=None)
    stub = patched_openai["make"]([boom, _reply("ok")])
    assert _client().generate("hi") == "ok"
    assert stub.temperatures == [0.0, RETRY_TEMPERATURE_STEP]


def test_a_bad_request_is_not_retried(patched_openai):
    """The other half of the contract: a 4xx that is not 429 fails the same way
    every time, so retrying it only multiplies the wait. It must surface at once.
    """
    resp = httpx.Response(400, request=httpx.Request("POST", "http://x/v1"))
    bad = BadRequestError("context length exceeded", response=resp, body=None)
    stub = patched_openai["make"]([bad, _reply("never reached")])
    with pytest.raises(BadRequestError):
        _client().generate("hi")
    assert stub.temperatures == [0.0]
