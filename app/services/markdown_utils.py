"""Shared repairs for Markdown a small model emits with glitches.

Used both when a report field is first written (report_generation) and on
every read (app.templating.render_markdown), since rows stored before a given
repair was added keep the raw glitch.
"""

import re


_PSEUDO_LIST = re.compile(r"\A[\{\[]\s*(?:\"[^\"]*\"|'[^']*')(?:\s*,\s*(?:\"[^\"]*\"|'[^']*'))*\s*[\}\]]\Z")
_QUOTED_ITEM = re.compile(r"\"([^\"]*)\"|'([^']*)'")

_DOUBLE_BULLET = re.compile(r"(?m)^(\s*)(?:[-*]\s+){2,}")

_EMPTY_ITEM = re.compile(r"(?m)^[ \t]*(?:[-*+]|\d+[.)])[ \t]*[*_`~ \t]*$\n?")


def clean_markdown(text: str) -> str:
    """Repair common Markdown glitches before rendering.

    Handles a {"a","b"} pseudo-list string and doubled bullet markers, both from
    the model, plus empty list items from the professor's own editor, where a
    stray Enter inside a list leaves a marker with no text.

    Args:
        text: Stored Markdown, possibly with glitches.

    Returns:
        The repaired Markdown.
    """
    stripped = text.strip()
    if _PSEUDO_LIST.match(stripped):
        items = [a or b for a, b in _QUOTED_ITEM.findall(stripped)]
        return "\n".join(f"- {item.strip()}" for item in items if item.strip())
    return _EMPTY_ITEM.sub("", _DOUBLE_BULLET.sub(r"\1- ", text))
