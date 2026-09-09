"""Client-chat history compaction.

The browser keeps chat transcripts in sessionStorage and sends them back on each
turn. When a transcript grows, the older part is summarized into one hidden
summary turn and the recent turns are kept verbatim. The task prompt for the
materials or report chat is never compacted, only conversation history is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import get_settings
from app.llm.client import NO_THINKING, get_generation_client
from app.llm.prompts import CHARS_PER_TOKEN, MENTION_RE, UPDATED_MARK_RE, render_prompt

_RECENT_TURNS_TO_KEEP = 6
_TARGET_WORDS = 650


@dataclass
class CompactResult:
    """The outcome of one compaction pass.

    Attributes:
        history: The history to use from now on, compacted or unchanged.
        compacted: Whether anything was actually summarized.
        compacted_until_at: Timestamp of the newest turn folded into the
            summary, so the browser knows what it may drop.
        message: One line for the client, shown or logged.
    """

    history: list[dict]
    compacted: bool = False
    compacted_until_at: int | None = None
    message: str = ""

    def as_payload(self) -> dict:
        """Serialise for the JSON response."""
        return {
            "history": self.history,
            "compacted": self.compacted,
            "compacted_until_at": self.compacted_until_at,
            "message": self.message,
        }


def history_char_threshold() -> int:
    """When to summarize browser history, derived from the model window.

    The report chat often has a large fixed prompt, findings plus editable
    blocks, so conversation memory gets only a modest slice of the context. The
    clamps bound the result to 12k-36k characters, so a tiny or absurd
    configured window cannot make the threshold degenerate.

    Returns:
        The transcript length, in characters, at which compaction kicks in.
    """
    settings = get_settings()
    budget = max(0, settings.generation_max_model_len - settings.chat_output_token_budget)
    chars = int(budget * CHARS_PER_TOKEN * 0.18)
    return min(36000, max(12000, chars))


def should_compact(history: list[dict]) -> bool:
    """Whether a transcript is long enough to be worth summarizing.

    Args:
        history: The turns as posted by the browser.

    Returns:
        True when there are older turns to fold in and the transcript has
        reached the threshold.
    """
    turns = [t for t in history if t.get("role") in ("user", "assistant")]
    if len(turns) <= _RECENT_TURNS_TO_KEEP:
        return False
    return _history_chars(history) >= history_char_threshold()


def compact_history(history: list[dict], chat_kind: str) -> CompactResult:
    """Summarize the older turns of a transcript, keeping the recent ones.

    Every failure, an unneeded pass, an unavailable model, a refused or empty
    answer, returns the original history with a message: the caller can always
    proceed with the chat, and prompt budgeting remains the final safety net.

    Args:
        history: The turns as posted by the browser.
        chat_kind: Which conversation this is, named in the summarizer prompt.

    Returns:
        The compacted history, or the original one unchanged.
    """
    history = _clean_history(history)
    if not should_compact(history):
        return CompactResult(history=history, message="No compaction needed.")

    summaries = [t for t in history if t.get("role") == "summary"]
    real_turns = [t for t in history if t.get("role") in ("user", "assistant")]
    old_turns = real_turns[:-_RECENT_TURNS_TO_KEEP]
    recent = real_turns[-_RECENT_TURNS_TO_KEEP:]
    if not old_turns:
        return CompactResult(history=history, message="No compaction needed.")

    client = get_generation_client()
    if not client.is_configured:
        return CompactResult(history=history, message="Compaction skipped: model unavailable.")

    prompt = render_prompt(
        "chat_compact.j2",
        chat_kind=chat_kind,
        now=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        summaries=summaries,
        turns=old_turns,
        target_words=_TARGET_WORDS,
    )
    try:
        summary = client.generate(
            prompt,
            extra_body={**NO_THINKING, "max_tokens": 1400},
        ).strip()
    except Exception:
        return CompactResult(history=history, message="Compaction failed; continuing with full history.")

    if not summary:
        return CompactResult(history=history, message="Compaction produced no summary.")

    summary = _append_deterministic_memory(summary, summaries + old_turns)
    cutoff = max((_turn_at(t) for t in old_turns), default=None)
    compacted_history = [
        {
            "role": "summary",
            "content": summary,
            "at": int(datetime.now(timezone.utc).timestamp() * 1000),
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        *recent,
    ]
    return CompactResult(
        history=compacted_history,
        compacted=True,
        compacted_until_at=cutoff,
        message="Older chat context was compacted for the next model call.",
    )


def _clean_history(history: list[dict]) -> list[dict]:
    """Keep only well-formed turns, normalising their timestamps."""
    out: list[dict] = []
    for t in history or []:
        if not isinstance(t, dict):
            continue
        role = t.get("role")
        content = t.get("content")
        if role not in ("user", "assistant", "summary") or not isinstance(content, str):
            continue
        item = {"role": role, "content": content}
        at = _turn_at(t)
        if at is not None:
            item["at"] = at
            item["time"] = _format_time(at)
        elif isinstance(t.get("time"), str):
            item["time"] = t["time"]
        out.append(item)
    return out


def _history_chars(history: list[dict]) -> int:
    """Rough transcript length, counting a fixed overhead per turn."""
    return sum(len(str(t.get("content", ""))) + 32 for t in history or [])


def _turn_at(turn: dict) -> int | None:
    """A turn's millisecond timestamp, or None when it has no usable one."""
    raw = turn.get("at")
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _format_time(ms: int) -> str:
    """Render a millisecond timestamp as ISO-8601, or "" when out of range."""
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def _append_deterministic_memory(summary: str, compacted: list[dict]) -> str:
    """Append the exact markers a summarizer must not paraphrase away.

    The edit records and @-mentions of the folded turns are re-stated verbatim,
    so a later turn can still refer to what was edited or which document was
    named.

    Args:
        summary: The model's summary of the older turns.
        compacted: The turns that were folded into it.

    Returns:
        The summary with the preserved markers appended.
    """
    edits: list[str] = []
    mentions: list[str] = []
    for turn in compacted:
        text = str(turn.get("content", ""))
        for mark in UPDATED_MARK_RE.findall(text):
            marker = f"[Updated: {mark.rstrip('.')}.]"
            if marker not in edits:
                edits.append(marker)
        for quoted, bare in MENTION_RE.findall(text):
            name = (quoted or bare).strip()
            if not name:
                continue
            mention = f'@"{name}"' if " " in name else f"@{name}"
            if mention not in mentions:
                mentions.append(mention)

    parts = [summary.strip()]
    if edits:
        parts.append("Exact edit markers preserved from compacted turns:\n" + "\n".join(edits))
    if mentions:
        parts.append("Document mentions preserved from compacted turns: " + ", ".join(mentions[-8:]))
    return "\n\n".join(p for p in parts if p)
