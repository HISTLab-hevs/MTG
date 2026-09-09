"""Prompt rendering and model-output decoding.

Prompts are Jinja2 templates in app/prompt/, one file per task, so they can
be reviewed and edited without touching Python code.
"""

import json
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.models import DocumentType

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompt"

# Rough chars-per-token for prompt budgeting only. English prose is ~4; the
# conservative 3.2 over-counts tokens slightly so callers clip a touch early
# rather than overflow the model.
CHARS_PER_TOKEN = 3.2

# Deliberately generous per-message ceiling, enforced by both chat services and
# exposed in the two textareas. It protects the fixed prompt budget without
# sending an oversized request to either model.
CHAT_MESSAGE_MAX_CHARS = 16_000

# Appended to clipped grounding text so the model never reads the cut point as
# the document's real end.
TRUNCATION_MARKER = "\n\n[document text truncated for length]"

# The "[Updated: <labels>.]" record the report-chat UI appends to an assistant
# turn that edited blocks (see syncHistory in report_view.html). Shared by
# report_chat, which renders the authoritative list, and chat_compaction, which
# preserves it verbatim across a summarization pass.
UPDATED_MARK_RE = re.compile(r"\[Updated: (.+?)\]")

# An @-mention of a document in the professor's message, e.g. @syllabus.pdf or
# @"two words.pdf". Shared by report_chat, which resolves mentions to a
# document, and chat_compaction, which preserves mentioned filenames verbatim.
# See report_chat._MENTION for the accepted forms.
MENTION_RE = re.compile(r'@"([^"]+)"|(?<!\w)@([\w.\-]+)')

_env = Environment(
    loader=FileSystemLoader(str(PROMPT_DIR)),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
# Render a stored (possibly multi-) document type as a human label, e.g.
# "assessment,rubric" -> "Assessment + Rubric", so prompts read naturally.
_env.filters["doc_type_label"] = DocumentType.label


def render_prompt(name: str, **context) -> str:
    """Render a prompt template.

    Args:
        name: Template filename under app/prompt/.
        **context: Template variables; a missing one raises, by StrictUndefined.

    Returns:
        The rendered prompt, stripped.
    """
    return _env.get_template(name).render(**context).strip()


def char_budget(max_model_len: int, output_token_budget: int) -> int:
    """Characters a prompt may use, leaving room for the model's own answer.

    Report generation and report chat reserve a different output allowance from
    the same context window, then convert the remaining tokens at the same
    calibration.

    Args:
        max_model_len: Context window of the model, in tokens.
        output_token_budget: Tokens reserved for the answer.

    Returns:
        The character budget for the prompt.
    """
    tokens = max(0, max_model_len - output_token_budget)
    return int(tokens * CHARS_PER_TOKEN)


def clip_markdown_to_budget(render, markdown: str, char_budget: int) -> str:
    """Render a prompt, clipping its markdown body if the result overflows.

    Only a genuinely oversized document is clipped, and only the markdown: the
    rest of the prompt (verdicts, quotes, findings) is never cut. The clip is
    marked so the model never reads the cut point as the document's real end.

    Args:
        render: Callable returning the full prompt for a given markdown body.
        markdown: Document text to embed in the prompt.
        char_budget: Maximum length of the rendered prompt.

    Returns:
        The rendered prompt, within budget.
    """
    prompt = render(markdown)
    if len(prompt) <= char_budget or not markdown:
        return prompt
    fixed_len = len(render(""))
    room = char_budget - fixed_len - len(TRUNCATION_MARKER)
    if room <= 0:
        # Pathological: even the fixed part fills the window. Drop the markdown
        # entirely, the rest of the prompt still produces a correct answer.
        return render("")
    return render(markdown[:room].rstrip() + TRUNCATION_MARKER)


def strip_code_fence(text: str) -> str:
    """Strip a Markdown code fence a model sometimes wraps its JSON answer in.

    Args:
        text: Raw model answer.

    Returns:
        The answer without the surrounding fence.
    """
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`")
        body = body.split("\n", 1)[1] if "\n" in body else body
    return body


# What may legally follow a backslash inside a JSON string ("u" is handled as a
# unit with its four hex digits by the repair scanner below).
_JSON_ESCAPES = set('"\\/bfnrtu')

# After a string-closing quote, JSON allows only a comma, colon, brace, bracket
# or the end of the text. After a comma a new key or value must start; checking
# that keeps a quote inside prose ("yes", he said) from ending the string early.
_RESUMES_JSON = re.compile(r'\s*(?:"|\{|\[|-?\d|true\b|false\b|null\b)')


def loads_model_json(answer: str):
    """Decode a model's JSON answer, repairing it when it is almost valid.

    vLLM's guided-JSON decoding pins the shape but does not stop the model from
    emitting a raw double quote, an invalid escape (LaTeX such as a
    single-backslash \\omega), or a raw newline inside a string, each of which
    makes json.loads fail on an answer whose content is fine. Rule text and
    document quotes make inner quotes common, so such an answer is re-scanned
    rather than discarded.

    Args:
        answer: Raw model answer, optionally inside a code fence.

    Returns:
        The decoded JSON value.

    Raises:
        ValueError: Even the repaired text does not parse.
    """
    body = strip_code_fence(answer)
    try:
        return json.loads(body)
    except ValueError:
        return json.loads(_repair_json(body))


def _repair_json(text: str) -> str:
    """Escape illegal content inside the strings of a nearly-JSON text.

    Structural characters outside strings are kept as they are.

    Args:
        text: Nearly-valid JSON.

    Returns:
        Text in which every string body is legally escaped.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if not in_string:
            if ch == '"':
                in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "\\":
            nxt = text[i + 1] if i + 1 < n else ""
            if nxt == "u" and re.match(r"u[0-9a-fA-F]{4}", text[i + 1 : i + 6]):
                out.append(text[i : i + 6])
                i += 6
            elif nxt in _JSON_ESCAPES:
                out.append(ch + nxt)
                i += 2
            else:  # lone or invalid escape, e.g. "\omega" -> "\\omega"
                out.append("\\\\")
                i += 1
            continue
        if ch == '"':
            if _closes_string(text, i + 1):
                in_string = False
                out.append(ch)
            else:
                out.append('\\"')
            i += 1
            continue
        if ch in "\n\r\t" or ord(ch) < 0x20:
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(ch, f"\\u{ord(ch):04x}"))
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _closes_string(text: str, j: int) -> bool:
    """Whether the quote before a position structurally ends its JSON string.

    Args:
        text: Text being repaired.
        j: Index just after the candidate closing quote.

    Returns:
        True when what follows is legal after a string.
    """
    k = j
    while k < len(text) and text[k] in " \t\r\n":
        k += 1
    if k >= len(text):
        return True
    c = text[k]
    if c in "}]:":
        return True
    if c == ",":
        return bool(_RESUMES_JSON.match(text, k + 1))
    return False
