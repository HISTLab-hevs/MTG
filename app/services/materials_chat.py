"""The materials assistant: a read-only RAG chat over a course's materials.

A turn retrieves the most relevant pages by cosine similarity over the page
embeddings, hands their numbered Markdown to the generation model, and returns
the grounded answer rendered to HTML with its inline citations turned into
source chips. Unlike the report assistant this never edits anything, so the call
is a plain text completion, not guided JSON.
"""

import logging
import re
from dataclasses import dataclass, field

from markupsafe import Markup, escape
from sqlalchemy.orm import Session

from app.llm.client import NO_THINKING, get_embedding_client, get_generation_client
from app.llm.prompts import CHAT_MESSAGE_MAX_CHARS, render_prompt
from app.services.embedding import RetrievedPage, retrieve
from app.templating import render_markdown

logger = logging.getLogger(__name__)

_CITATION = re.compile(r"\[(\d+)\]")


@dataclass
class Source:
    """One cited page, as shown in the answer's Sources list."""

    number: int
    filename: str
    page_number: int


@dataclass
class ChatResult:
    """One turn's answer.

    Attributes:
        reply_html: The rendered answer, with its citations as chips.
        sources: The cited pages only, never the whole retrieved set.
    """

    reply_html: Markup
    sources: list[Source] = field(default_factory=list)


def run_materials_chat(
    db: Session, course_id: int, message: str, history: list[dict] | None = None
) -> ChatResult:
    """Answer one question, grounded in the course's most relevant pages.

    Every failure mode, an unconfigured model, a retrieval or model error, an
    empty course, comes back as a readable answer rather than an exception: the
    result is swapped straight into the chat log.

    Args:
        db: Session used for retrieval.
        course_id: Course whose materials to search.
        message: The professor's question.
        history: Prior turns, used to keep a short follow-up on topic.

    Returns:
        The rendered answer with its cited sources.
    """
    if len(message) > CHAT_MESSAGE_MAX_CHARS:
        return ChatResult(Markup(
            "<p>That message is too long. The maximum is 16,000 characters. "
            "Please shorten it or split it into separate questions.</p>"
        ))
    client = get_generation_client()
    if not client.is_configured:
        return ChatResult(
            Markup("<p>The assistant is unavailable: no generation model is "
                   "configured.</p>")
        )
    if not get_embedding_client().is_configured:
        return ChatResult(
            Markup("<p>The assistant is unavailable: no embedding model is "
                   "configured, so the course materials cannot be searched.</p>")
        )

    try:
        passages = retrieve(db, course_id, _retrieval_query(message, history))
    except Exception:
        logger.exception("Materials chat retrieval failed")
        return ChatResult(
            Markup("<p>The assistant is temporarily unavailable (the course "
                   "materials could not be searched). Please try again in a "
                   "moment.</p>")
        )
    if not passages:
        return ChatResult(
            Markup("<p>I couldn't find anything in this course's materials to "
                   "answer that. The materials may still be processing.</p>")
        )

    prompt = render_prompt(
        "materials_chat.j2",
        passages=passages,
        message=message,
        history=history or [],
    )
    try:
        raw = client.generate(prompt, extra_body=NO_THINKING).strip()
    except Exception:
        logger.exception("Materials chat model call failed")
        return ChatResult(
            Markup("<p>The assistant is temporarily unavailable (the model did "
                   "not respond). Please try again in a moment.</p>")
        )
    if not raw:
        return ChatResult(Markup("<p>No answer.</p>"))

    return _build_result(raw, passages)


def _retrieval_query(message: str, history: list[dict] | None) -> str:
    """The text to embed for retrieval, with recent context folded in.

    A bare "testnet?" retrieves near-random pages on its own, since the topic
    lives in the previous turn rather than in these few words. Prepending the
    last exchange carries that topic into the embedding; a message that already
    stands alone is barely affected, as cosine retrieval stays dominated by the
    longer on-topic text either way.

    Args:
        message: The professor's question.
        history: Prior turns; the last exchange is folded in.

    Returns:
        The text to embed.
    """
    if not history:
        return message
    last = [t for t in history if t.get("role") in ("user", "assistant")][-2:]
    if not last:
        return message
    context = " ".join(str(t.get("content", "")) for t in last)
    return f"{context} {message}"


def _build_result(raw: str, passages: list[RetrievedPage]) -> ChatResult:
    """Render the answer to HTML and renumber its citations from 1.

    The model cites passages by their position in the retrieved set, which may
    skip numbers: it might use [4] and [5] of 6. Each cited passage is remapped
    to a compact 1..N in first-citation order, so the reader sees [1] and [2]
    matching a short Sources list instead of the raw retrieval indices.

    Args:
        raw: The model's answer, with its citation markers.
        passages: The retrieved pages the markers index into.

    Returns:
        The rendered answer with its cited sources, or a readable fallback when
        the answer carries no valid citation.
    """
    cited = [int(token) for token in _CITATION.findall(raw)]
    if not cited or any(idx < 1 or idx > len(passages) for idx in cited):
        logger.warning("Materials chat returned an answer without valid citations")
        return ChatResult(Markup(
            "<p>I couldn't produce a source-backed answer for that question. "
            "Please try rephrasing it.</p>"
        ))

    display: dict[int, int] = {}
    for idx in cited:
        if idx not in display:
            display[idx] = len(display) + 1

    sources = [
        Source(num, passages[idx - 1].filename, passages[idx - 1].page_number)
        for idx, num in display.items()
    ]

    html = str(render_markdown(raw))

    def chip(match: re.Match) -> str:
        idx = int(match.group(1))
        if idx not in display:
            return match.group(0)
        p = passages[idx - 1]
        title = escape(f"{p.filename}, page {p.page_number}")
        return f'<sup class="cite" title="{title}">{display[idx]}</sup>'

    html = _CITATION.sub(chip, html)
    return ChatResult(Markup(html), sources)
