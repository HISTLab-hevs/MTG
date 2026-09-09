"""Inline page scripts must not call app.js helpers at parse time.

app.js is deferred, so it runs after the document is parsed: an inline call at
parse time throws, taking every handler in the enclosing block with it. Calls
must sit in a DOMContentLoaded listener or in a fragment htmx swaps in later.
"""

import re
from pathlib import Path

import pytest

TEMPLATES = Path("app/templates")
APP_JS = Path("app/static/js/app.js")
# Read out of app.js rather than listed here, so a helper added there is covered
# without anyone remembering to update this file.
HELPERS = tuple(sorted(re.findall(
    r"^function +([A-Za-z_][A-Za-z0-9_]*)",
    APP_JS.read_text(encoding="utf-8"),
    re.M,
)))
# Fragments htmx inserts after load; their scripts run once app.js is available.
SWAPPED_IN_LATER = {"_rule_edit_modal.html", "_course_edit_modal.html"}


def _runs_during_parse(script: str, helper: str) -> bool:
    """Would this call run while the page is still parsing?

    Walks the script and tracks whether the current position sits inside a
    callback body, i.e. inside `function (` / `=> {` / `addEventListener(`.
    A call at nesting depth 0 relative to those runs immediately; one inside a
    callback runs whenever that callback fires, which is later.
    """
    for match in re.finditer(rf"\b{helper}\s*\(", script):
        before = script[: match.start()]
        # An IIFE body still runs during parsing, so `(function () {` does NOT
        # count as deferral; only a callback PASSED to something does.
        deferring = len(re.findall(
            r"(?:addEventListener|then|setTimeout|requestAnimationFrame)\s*\(", before
        ))
        # Count how many of those callbacks have already been closed.
        if deferring == 0:
            return True
        # Inside a DOMContentLoaded (or similar) callback that has not closed
        # before this point: find the last one and check it is still open.
        last = list(re.finditer(
            r"(?:addEventListener|then|setTimeout|requestAnimationFrame)\s*\(", before
        ))[-1]
        tail = script[last.end() : match.start()]
        if tail.count("{") <= tail.count("}"):
            return True
    return False


@pytest.mark.parametrize(
    "path",
    sorted(p for p in TEMPLATES.rglob("*.html") if p.name not in SWAPPED_IN_LATER),
    ids=lambda p: str(p.relative_to(TEMPLATES)),
)
def test_app_js_helpers_are_called_after_the_page_loads(path):
    html = path.read_text(encoding="utf-8")
    for script in re.findall(r"<script>(.*?)</script>", html, re.S):
        for helper in HELPERS:
            assert not _runs_during_parse(script, helper), (
                f"{path}: {helper}() is called while the page is parsing, but "
                "app.js is deferred and has not defined it yet. Wrap the call "
                "in a DOMContentLoaded listener."
            )
