"""Settings read from the environment, and the per-task LLM clients."""

import pytest

from app.config import get_settings


def test_page_extraction_concurrency_is_read_from_env(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("PAGE_EXTRACTION_CONCURRENCY", "8")

    try:
        assert get_settings().page_extraction_concurrency == 8
    finally:
        get_settings.cache_clear()


def test_missing_extraction_config_fails_fast(monkeypatch):
    from pathlib import Path

    from app.services.extraction import extract_page_markdown

    get_settings.cache_clear()
    monkeypatch.setenv("EXTRACTION_BASE_URL", "")
    monkeypatch.setenv("EXTRACTION_MODEL", "")

    try:
        with pytest.raises(RuntimeError, match="EXTRACTION_BASE_URL and EXTRACTION_MODEL"):
            extract_page_markdown(Path("does-not-need-to-exist.png"), 1)
    finally:
        get_settings.cache_clear()


def test_llm_clients_are_selected_by_task(monkeypatch):
    from app.llm.client import get_extraction_client, get_generation_client

    get_settings.cache_clear()
    monkeypatch.setenv("EXTRACTION_BASE_URL", "http://extract.local:8000/v1")
    monkeypatch.setenv("EXTRACTION_API_KEY", "extract-key")
    monkeypatch.setenv("EXTRACTION_MODEL", "extract-model")
    monkeypatch.setenv("GENERATION_BASE_URL", "http://generate.local:8001/v1")
    monkeypatch.setenv("GENERATION_API_KEY", "generate-key")
    monkeypatch.setenv("GENERATION_MODEL", "generate-model")

    try:
        extraction = get_extraction_client()
        generation = get_generation_client()

        assert extraction.task == "extraction"
        assert extraction.base_url == "http://extract.local:8000/v1"
        assert extraction.api_key == "extract-key"
        assert extraction.model == "extract-model"
        assert generation.task == "generation"
        assert generation.base_url == "http://generate.local:8001/v1"
        assert generation.api_key == "generate-key"
        assert generation.model == "generate-model"
    finally:
        get_settings.cache_clear()
