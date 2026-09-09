"""Shared LLM access layer.

Every model call in the application goes through LLMClient. Task helpers pick
the right OpenAI-compatible profile so extraction and generated content can
use different endpoints without leaking configuration into service code.
"""

import base64

from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from app.config import get_settings

# Failures that can clear on a second attempt. A 4xx other than 429, or a
# context overflow, is deliberately absent: retrying would fail identically.
_RETRYABLE = (APITimeoutError, APIConnectionError, InternalServerError, RateLimitError)

# At temperature 0 a request is deterministic, so a retry must change sampling
# to escape a divergence; a small step keeps the output close to the intent.
RETRY_TEMPERATURE_STEP = 0.1

# extra_body for single-shot tasks (JSON extraction, chat replies, CEL
# generation), never multi-step reasoning: thinking would only add latency.
NO_THINKING = {"chat_template_kwargs": {"enable_thinking": False}}


class LLMClient:
    """An OpenAI-compatible endpoint bound to one task profile.

    Attributes:
        task: Profile name, used in configuration errors.
        model: Model identifier sent with every request.
        base_url: Endpoint URL including /v1; empty when unconfigured.
        request_timeout: Per-attempt wall-clock cap, in seconds.
        max_retries: Retries after the first attempt on a transient failure.
    """

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        task: str = "generation",
        base_url_env: str = "GENERATION_BASE_URL",
        model_env: str = "GENERATION_MODEL",
        request_timeout: float = 180.0,
        max_retries: int = 2,
    ):
        """Bind a task profile.

        Args:
            model: Model identifier.
            base_url: Endpoint URL including /v1.
            api_key: Endpoint key; vLLM accepts any non-empty value.
            task: Profile name, used in configuration errors.
            base_url_env: Environment variable named in the error when base_url
                is missing.
            model_env: Environment variable named in the error when model is
                missing.
            request_timeout: Per-attempt wall-clock cap, in seconds.
            max_retries: Retries after the first attempt.
        """
        self.task = task
        self.model = model
        self.base_url = base_url or ""
        self.api_key = api_key or "EMPTY"
        self.base_url_env = base_url_env
        self.model_env = model_env
        self.request_timeout = request_timeout
        self.max_retries = max_retries
        self._clients: dict[tuple[float, int], OpenAI] = {}

    @property
    def is_configured(self) -> bool:
        """Whether both an endpoint and a model are set."""
        return bool(self.base_url and self.model)

    def _openai(self, max_retries: int) -> OpenAI:
        """Return the SDK client for this (timeout, retries) pair, built once.

        An analysis run issues one call per page; a client per call would mean
        a new connection pool, and so a new handshake, for each one. Keyed on
        both values because generate handles its own retries while embed
        delegates them to the SDK, and report_chat raises request_timeout after
        construction.

        Args:
            max_retries: Retries the SDK itself should perform.

        Returns:
            A cached client, created on first use for this key.
        """
        key = (self.request_timeout, max_retries)
        client = self._clients.get(key)
        if client is None:
            client = OpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=self.request_timeout,
                max_retries=max_retries,
            )
            self._clients[key] = client
        return client

    def generate(
        self,
        prompt: str | None,
        image_png: bytes | None = None,
        temperature: float = 0.0,
        extra_body: dict | None = None,
    ) -> str:
        """Run one chat completion, retrying transient failures.

        Under guided decoding a model occasionally never emits a stop token and
        generates until the context window fills, so request_timeout caps each
        attempt rather than letting it hang until the RQ job timeout. Retries
        are handled here, not by the SDK, to raise the temperature each time.

        Args:
            prompt: Text part of the message; may be omitted for image-only
                requests.
            image_png: PNG bytes sent alongside the prompt as a data URL.
            temperature: Sampling temperature of the first attempt.
            extra_body: Extra request fields, such as guided decoding or
                NO_THINKING.

        Returns:
            The model's answer, stripped.

        Raises:
            RuntimeError: The endpoint or the model is not configured.
            openai.APIError: Every attempt failed transiently; the last error
                is re-raised.
        """
        if not self.base_url:
            raise RuntimeError(
                f"LLM endpoint for {self.task} is not configured. Set "
                f"{self.base_url_env} to the OpenAI-compatible endpoint, "
                "including /v1."
            )
        if not self.model:
            raise RuntimeError(
                f"LLM model for {self.task} is not configured. Set {self.model_env}."
            )

        content: list[dict] = []
        if prompt:
            content.append({"type": "text", "text": prompt})
        if image_png is not None:
            image_b64 = base64.b64encode(image_png).decode()
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                }
            )
        client = self._openai(max_retries=0)
        messages = [{"role": "user", "content": content}]
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    temperature=temperature + attempt * RETRY_TEMPERATURE_STEP,
                    messages=messages,
                    extra_body=extra_body or {},
                )
                return (response.choices[0].message.content or "").strip()
            except _RETRYABLE as exc:
                last_error = exc
        raise last_error

    def embed(self, inputs: list[str]) -> list[list[float]]:
        """Embed a batch of texts via the OpenAI-compatible /v1/embeddings.

        Args:
            inputs: Texts to embed.

        Returns:
            One vector per input, in the same order.

        Raises:
            RuntimeError: The endpoint or the model is not configured.
        """
        if not self.is_configured:
            raise RuntimeError(
                f"Embedding endpoint for {self.task} is not configured. Set "
                f"{self.base_url_env} and {self.model_env}."
            )
        client = self._openai(max_retries=self.max_retries)
        response = client.embeddings.create(model=self.model, input=inputs)
        return [item.embedding for item in response.data]


def get_task_client(task: str) -> LLMClient:
    """Build the client for a task profile from settings.

    Args:
        task: Profile name; settings and environment variables are derived from
            it, as in "extraction" -> extraction_model, EXTRACTION_MODEL.

    Returns:
        A client bound to that profile, configured or not.
    """
    settings = get_settings()
    prefix = task.upper()
    return LLMClient(
        task=task,
        model=getattr(settings, f"{task}_model"),
        base_url=getattr(settings, f"{task}_base_url"),
        api_key=getattr(settings, f"{task}_api_key"),
        base_url_env=f"{prefix}_BASE_URL",
        model_env=f"{prefix}_MODEL",
        request_timeout=settings.llm_request_timeout,
        max_retries=settings.llm_max_retries,
    )


def get_generation_client() -> LLMClient:
    """Return the client for generated content."""
    return get_task_client("generation")


def get_rules_client() -> LLMClient:
    """Return the client for rule compilation, currently the generation one."""
    return get_generation_client()


def get_report_client() -> LLMClient:
    """Return the client for report analysis, currently the generation one."""
    return get_generation_client()


def get_extraction_client() -> LLMClient:
    """Return the client for page extraction, on the vision model."""
    return get_task_client("extraction")


def get_embedding_client() -> LLMClient:
    """Return the client for embeddings."""
    return get_task_client("embedding")
