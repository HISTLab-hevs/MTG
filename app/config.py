"""Application settings, read once from the environment and the .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Every setting the app reads, with the defaults a local run can use.

    Each field is overridden by the matching environment variable, upper-cased.
    The three model profiles (generation, extraction, embedding) are read by
    name in llm.client.get_task_client, so their fields must keep the
    <task>_base_url, <task>_api_key and <task>_model shape.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_secret_key: str = "dev-secret-change-me"

    session_idle_minutes: int = 30

    https_only: bool = True
    database_url: str = "postgresql+psycopg://mtg:mtg@localhost:5432/mtg"
    redis_url: str = "redis://localhost:6379/0"
    storage_dir: str = "./storage"

    generation_base_url: str = ""
    generation_api_key: str = "EMPTY"
    generation_model: str = ""

    extraction_base_url: str = ""
    extraction_api_key: str = "EMPTY"
    extraction_model: str = ""

    embedding_base_url: str = ""
    embedding_api_key: str = "EMPTY"
    embedding_model: str = ""

    embedding_batch_size: int = 8

    materials_retrieval_k: int = 8

    extraction_mode: str = "prompt"

    max_upload_mb: int = 50
    pdf_render_dpi: int = 150
    page_extraction_concurrency: int = 1

    evidence_extraction_concurrency: int = 8

    report_generation_concurrency: int = 8

    llm_request_timeout: float = 120.0
    llm_max_retries: int = 2

    # Context window of the generation model, in tokens. The two budgets below
    # reserve part of it for the answer; see llm.prompts.char_budget.
    generation_max_model_len: int = 65536

    report_output_token_budget: int = 8192

    chat_output_token_budget: int = 16384

    # Run background jobs inline in the calling process. Used by the tests;
    # production goes through Redis and the RQ workers.
    job_eager: bool = False

    @property
    def max_upload_bytes(self) -> int:
        """The upload limit in bytes, from the configured megabytes."""
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    """The settings, read once and cached for the process."""
    return Settings()
