"""Per-page evidence extraction for the rule engine.

For one document, the union of its local rules' contract fields defines a JSON
schema. Each page is sent to the generation model with the page image as ground
truth and the page markdown as a support aid; constrained decoding forces the
schema, and every field is defaulted afterwards so aggregation and celpy never
see a missing key. One request per page keeps traceability and retries cheap.

This deliberately uses the generation client, not the extraction one: the
extraction profile (app/services/extraction.py) may point at a template-only
model such as NuExtract, which ignores natural-language prompts and so cannot
make the per-field boolean judgements this prompt asks for. Thinking is always
off, since these are independent per-field judgements, not a chain-of-thought
task.
"""

import json
import logging

from app.llm.client import NO_THINKING, get_generation_client
from app.llm.prompts import loads_model_json, render_prompt
from app.services import storage
from app.services.contracts import TYPE_DEFAULT, Field, schema_for_fields

logger = logging.getLogger(__name__)


def extract_page_facts(
    image_path: str, page_markdown: str, page_number: int, fields: list[Field]
) -> dict:
    """Extract one fact object for a page, defaulted to the field schema.

    Args:
        image_path: Stored PNG of the page, the ground truth for the model.
        page_markdown: Its transcription, passed as a support aid.
        page_number: Its 1-based number, recorded on every fact.
        fields: Contract fields to extract, from the document's local rules.

    Returns:
        One entry per field, as {applicable, value, quote, page}.

    Raises:
        RuntimeError: The generation endpoint is not configured.
    """
    client = get_generation_client()
    if not client.is_configured:
        raise RuntimeError(
            "Evidence extraction requires GENERATION_BASE_URL and GENERATION_MODEL."
        )
    schema = schema_for_fields(fields)
    prompt = render_prompt(
        "evidence_extraction.j2",
        schema=json.dumps(schema, indent=2),
        fields=fields,
        page_number=page_number,
        markdown=page_markdown,
    )
    answer = client.generate(
        prompt,
        image_png=storage.abs_path(image_path).read_bytes(),
        extra_body={**NO_THINKING, "guided_json": schema},
    )
    return _coerce(answer, fields, page_number)


def _coerce(
    answer: str, fields: list[Field], page_number: int
) -> dict:
    """Parse the model's answer into one defaulted entry per field.

    Quotes are kept as returned, never validated against the page markdown: the
    image is the ground truth and the markdown a lossy transcription that clips
    words at line and page boundaries. Dropping an image-grounded quote because
    the transcription lacks it would push grounding back onto the very
    transcription it is meant to correct, letting only its artefacts through.

    Args:
        answer: The model's raw answer.
        fields: Contract fields the schema asked for.
        page_number: Recorded on every fact.

    Returns:
        One entry per field, each value defaulted to its type so celpy never
        sees a missing key. A page whose answer would not parse is defaulted
        whole and marked with a __defaulted__ key.
    """
    defaulted = False
    try:
        data = loads_model_json(answer)
    except ValueError:
        logger.warning(
            "Evidence extractor returned non-JSON for page %s; defaulting page facts",
            page_number,
        )
        data = {}
        defaulted = True
    facts: dict = {}
    for f in fields:
        entry = data.get(f.name) if isinstance(data, dict) else None
        entry = entry if isinstance(entry, dict) else {}
        value = entry.get("value")
        quote = entry.get("quote") or ""
        applicable = entry.get("applicable")
        if not isinstance(applicable, bool):
            applicable = bool(quote) or bool(value)
        elif applicable and not quote and not value:
            applicable = False
        facts[f.name] = {
            "applicable": applicable,
            "value": value if value is not None else TYPE_DEFAULT[f.type],
            "quote": quote,
            "page": page_number,
        }
    if defaulted:
        facts["__defaulted__"] = True
    return facts
