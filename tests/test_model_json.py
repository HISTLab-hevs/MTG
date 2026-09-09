"""loads_model_json: decoding the almost-valid JSON guided decoding lets through.

vLLM's guided-JSON pins the shape but the model can still emit a raw double
quote, an invalid escape (single-backslash LaTeX), or a raw newline inside a
string. These used to make json.loads fail and the caller discard an otherwise
good answer — in the report chat that surfaced as "nothing was changed" on an
edit the model had actually written.
"""

import pytest

from app.llm.prompts import loads_model_json


def test_strict_json_passes_through():
    assert loads_model_json('{"a": [1, 2], "b": {"c": "d"}}') == {
        "a": [1, 2],
        "b": {"c": "d"},
    }


def test_code_fence_is_stripped():
    assert loads_model_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_unescaped_inner_quotes_are_repaired():
    # The exact live failure: the model quoted the source document verbatim.
    raw = '{"markdown": "For "Every time a client sends a request" see p.11"}'
    assert loads_model_json(raw) == {
        "markdown": 'For "Every time a client sends a request" see p.11'
    }


def test_inner_quote_followed_by_comma_stays_inside_string():
    # `", "` inside prose must not be read as the end of the value.
    assert loads_model_json('{"a": ""yes", he said", "b": 1}') == {
        "a": '"yes", he said',
        "b": 1,
    }


def test_single_backslash_latex_is_repaired():
    raw = '{"a": "trace $b^* a^\\omega$ and $\\sigma$"}'
    assert loads_model_json(raw) == {"a": "trace $b^* a^\\omega$ and $\\sigma$"}


def test_raw_newline_and_tab_inside_string_are_repaired():
    assert loads_model_json('{"a": "line1\nline2\there"}') == {
        "a": "line1\nline2\there"
    }


def test_valid_escapes_are_not_double_escaped():
    raw = '{"a": "already \\"fine\\" with \\n and \\u03c9"}'
    assert loads_model_json(raw) == {"a": 'already "fine" with \n and ω'}


def test_unrepairable_text_still_raises():
    with pytest.raises(ValueError):
        loads_model_json("I could not produce JSON, sorry.")
