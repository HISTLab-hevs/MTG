"""The Jinja environment and everything the templates call.

Registers the filters and globals the templates use, so a template never has to
import from the services. The Markdown renderer here is the one place a report's
stored text becomes HTML, which is why the escaping it does is load-bearing.
"""

import re
from pathlib import Path

import markdown as md
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app.models import DocumentType, RuleCategory
from app.services.markdown_utils import clean_markdown

TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


_LATEX_COMMANDS = {
    r"\times": "×", r"\cdot": "·", r"\div": "÷", r"\pm": "±",
    r"\leq": "≤", r"\geq": "≥", r"\neq": "≠", r"\approx": "≈",
    r"\rightarrow": "→", r"\to": "→", r"\Rightarrow": "⇒",
    r"\alpha": "α", r"\beta": "β", r"\Delta": "Δ", r"\delta": "δ",
    r"\%": "%", r"\$": "$", r"\&": "&", r"\#": "#", r"\,": " ", r"\;": " ",
}
_SUBSCRIPT = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")
_SUPERSCRIPT = str.maketrans("0123456789+-=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ")

_TEXT_WRAP = re.compile(r"\\(?:text|mathrm|mathbf|mathit|textbf|textit)\s*\{([^{}]*)\}")

_SUBSUP = re.compile(r"([_^])(?:\{([^{}]*)\}|(.))")

_MATH_SPAN = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$", re.S)


def _demath(expr: str) -> str:
    """Turn one LaTeX math expression into plain text, with no typesetting."""
    expr = _TEXT_WRAP.sub(lambda m: m.group(1), expr)

    def _script(m: re.Match) -> str:
        body = m.group(2) if m.group(2) is not None else m.group(3)
        table = _SUBSCRIPT if m.group(1) == "_" else _SUPERSCRIPT
        converted = body.translate(table)
        return converted if converted != body or body.isdigit() else body

    expr = _SUBSUP.sub(_script, expr)
    for cmd, repl in _LATEX_COMMANDS.items():
        expr = expr.replace(cmd, repl)

    expr = re.sub(r"\\[A-Za-z]+", "", expr)
    expr = expr.replace("{", "").replace("}", "")
    return re.sub(r"[ \t]{2,}", " ", expr).strip()


_MD_ESCAPE_ONLY = re.compile(r"\\[\\`*_{}\[\]()#+\-.!>~=|]")


def strip_latex(text: str) -> str:
    """Replace LaTeX math spans with their plain-text reading.

    So a professor reads "60 × 0.20 kg CO₂" rather than the raw commands the
    model sometimes emits. Markdown's own escapes are left intact: they are the
    only backslashes a professor's edit sends here, and unescaping them would
    turn their prose into headings.
    """
    if not text or "$" not in text and "\\" not in text:
        return text or ""

    text = _MATH_SPAN.sub(lambda m: _demath(m.group(1) or m.group(2) or ""), text)

    if "\\" in text and _MD_ESCAPE_ONLY.sub("", text).count("\\"):
        text = _demath(text)
    return text


def render_markdown(text: str | None) -> Markup:
    """Render a report's Markdown block to safe HTML.

    Raw HTML never passes through, and that escaping must stay. The result is
    wrapped in Markup, so it is inserted into the page unescaped, while the
    inputs are a model's output and the professor's own edits, neither of them
    trusted markup. Nothing in a report needs to author HTML, so the angle
    brackets are escaped before parsing and only the tags the Markdown parser
    itself emits survive.

    LaTeX is flattened first, and the same Markdown repairs that run on write
    run again here, since rows stored before a given repair keep the glitch.

    Args:
        text: The stored Markdown, or None.

    Returns:
        The HTML, safe to insert into the page.
    """
    source = clean_markdown(strip_latex(text or ""))
    source = source.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return Markup(
        md.markdown(source, extensions=["extra", "sane_lists"])
    )

STATUS_LABELS = {
    "uploaded": "Queued",
    "rendering": "Rendering pages",
    "extracting": "Extracting text",
    "embedding": "Indexing for search",
    "ready": "Ready",
    "failed": "Failed",
    "pending": "Pending",
    "generating": "Generating",
    "proposed": "Proposed",
    "running": "Analyzing",
    "reporting": "Writing report",
    "completed": "Completed",
}


DOCUMENT_TYPE_LABELS = DocumentType.LABELS


AGG_LABELS = {
    "any": "yes if at least one page shows it",
    "all": "yes only if every page that mentions it shows it",
    "pages": "the list of page numbers where it appears",
    "sum": "all pages added together",
    "max": "the highest value on any page",
    "min": "the lowest value on any page",
    "first": "the first value found",
    "union": "every distinct item, from all pages",
    "tagged_pages": "each item with the page where it appears",
}

AGG_LABELS_BY_TYPE = {
    ("sum", "boolean"): "on how many pages it is true",
}


def agg_label(agg: str, field_type: str | None = None) -> str:
    """Describe an aggregation in the words the edit dialog shows.

    Args:
        agg: The aggregation name.
        field_type: The field's per-page type, which changes the wording for
            some pairs.

    Returns:
        The description, falling back to the raw name.
    """
    return AGG_LABELS_BY_TYPE.get((agg, field_type), AGG_LABELS.get(agg, agg))

def _radar_svg(metrics):
    """The report's radar chart as inline SVG, or None when it has no data."""
    from app.services.report_charts import radar_svg

    svg = radar_svg(metrics) if metrics else None
    return Markup(svg) if svg else None


def _severity_bar_svg(metrics):
    """The report's severity bars as inline SVG, or None when nothing failed."""
    from app.services.report_charts import severity_bar_svg

    svg = severity_bar_svg(metrics) if metrics else None
    return Markup(svg) if svg else None


def _sev_letter(severity: str | None) -> str:
    """Severity as the single letter shown in the badges.

    One letter keeps the badge narrow enough to sit in a column beside a rule
    code without pushing the rule text around. The templates still give the full
    word to a screen reader and to the tooltip, so the letter never carries the
    meaning alone.
    """
    return (severity or "").strip()[:1].upper()


def _rule_sources(code: str | None) -> list[dict]:
    """The numbered references behind a default rule, for the rules list."""
    from app.rule_sources import NUMBERS, sources_for

    return [
        {"number": NUMBERS[s.key], "title": s.title}
        for s in sources_for(code)
    ]


templates.env.globals["status_label"] = lambda s: STATUS_LABELS.get(s, s)
templates.env.globals["sev_letter"] = _sev_letter
templates.env.globals["rule_sources"] = _rule_sources
templates.env.globals["icon"] = templates.env.get_template("_icons.html").module
templates.env.globals["agg_label"] = agg_label
templates.env.globals["category_label"] = RuleCategory.label
templates.env.globals["doc_type_label"] = DocumentType.label
templates.env.globals["doc_type_describe"] = DocumentType.describe
templates.env.globals["radar_svg"] = _radar_svg
templates.env.globals["severity_bar_svg"] = _severity_bar_svg
templates.env.filters["markdown"] = render_markdown
templates.env.filters["doc_type_label"] = DocumentType.label
templates.env.filters["doc_types"] = DocumentType.parse
