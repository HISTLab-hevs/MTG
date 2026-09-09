"""VLM page extraction.

One model request per page, through the shared LLM client (app/llm) with the
prompt template app/prompt/page_extraction.j2. Per-page requests keep
traceability clean, make retries cheap, and let extraction reuse work at
page level.
"""

from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.llm.client import LLMClient, get_extraction_client
from app.llm.prompts import render_prompt


@dataclass
class PageExtractionResult:
    """One page's extracted Markdown and the model that produced it."""

    page_number: int
    markdown: str
    model_name: str


def extract_page_markdown(image_path: Path, page_number: int) -> PageExtractionResult:
    """Extract one page image to Markdown.

    Args:
        image_path: Stored PNG of the page.
        page_number: Its 1-based number, used for the page anchor.

    Returns:
        The extracted Markdown with the model name.

    Raises:
        RuntimeError: The extraction endpoint is unconfigured, or the model
            returned nothing.
    """
    client = get_extraction_client()
    if not client.is_configured:
        raise RuntimeError(
            "Page extraction requires EXTRACTION_BASE_URL and EXTRACTION_MODEL. "
            "Set the endpoint including /v1, for example "
            "http://<vllm-host-ip>:8000/v1."
        )

    image_png = image_path.read_bytes()
    if get_settings().extraction_mode == "nuextract-markdown":
        markdown = _extract_nuextract_markdown(client, image_png, page_number)
    else:
        prompt = render_prompt("page_extraction.j2", page_number=page_number)
        markdown = client.generate(prompt, image_png=image_png)
    if not markdown:
        raise RuntimeError("The VLM returned an empty response for this page.")
    return PageExtractionResult(page_number, markdown, client.model)


def _extract_nuextract_markdown(
    client: LLMClient, image_png: bytes, page_number: int
) -> str:
    """Extract a page through NuExtract3's native document-to-Markdown mode.

    Per the model card: no text prompt, temperature 1, and the mode selected
    through vLLM chat_template_kwargs. The model does not emit the page anchor,
    so it is prepended here to keep page traceability.

    Args:
        client: Configured extraction client.
        image_png: PNG bytes of the page.
        page_number: Its 1-based number, used for the page anchor.

    Returns:
        The Markdown with its page anchor, or the empty string.
    """
    markdown = client.generate(
        None,
        image_png=image_png,
        temperature=1.0,
        extra_body={
            "chat_template_kwargs": {"mode": "markdown", "enable_thinking": False}
        },
    )
    if not markdown:
        return markdown
    return f"<!-- page: {page_number} -->\n\n{markdown}"

