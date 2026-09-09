"""Render a written course report to a downloadable PDF with ReportLab.

The report is stored as Markdown blocks: the course summary and suggestions,
then one section per document with its issues and suggestions. The PDF is built
from scratch rather than by rendering the web HTML, so there is no system
dependency: each block is converted to a flat list of ReportLab flowables.

The conversion handles only the subset of Markdown the report model emits,
headings, paragraphs, bullet and numbered lists, and inline bold, italic and
code, which is all the report's own Markdown renderer ever produces for these
blocks.
"""

import html
import io
import re

from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from app.models import Course, CourseReport
from app.services.report_metrics import parse_metrics

# Inline Markdown the report model emits, translated to the mini-HTML ReportLab
# parses. Single underscores are deliberately not italics: the model routinely
# writes snake_case field names such as has_rule_or_require, and the old rule
# turned those into stray, badly-nested <i> tags that crashed the strict
# ReportLab parser. Italics are still available through single asterisks.
_INLINE = [
    (re.compile(r"\*\*(.+?)\*\*"), r"<b>\1</b>"),
    (re.compile(r"__(.+?)__"), r"<b>\1</b>"),
    (re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"), r"<i>\1</i>"),
    (re.compile(r"`(.+?)`"), r'<font face="Courier">\1</font>'),
]


def _styles() -> dict[str, ParagraphStyle]:
    """The paragraph styles the report uses, keyed by role."""
    base = getSampleStyleSheet()
    body = ParagraphStyle(
        "ReportBody",
        parent=base["BodyText"],
        fontSize=10,
        leading=14,
        spaceAfter=6,
        alignment=TA_LEFT,
    )
    return {
        "title": ParagraphStyle(
            "ReportTitle", parent=base["Title"], fontSize=20, spaceAfter=4
        ),
        "h1": ParagraphStyle(
            "ReportH1", parent=base["Heading1"], fontSize=15, spaceBefore=14,
            spaceAfter=6,
        ),
        "h2": ParagraphStyle(
            "ReportH2", parent=base["Heading2"], fontSize=12, spaceBefore=10,
            spaceAfter=4,
        ),
        "label": ParagraphStyle(
            "ReportLabel", parent=base["Heading3"], fontSize=10, spaceBefore=8,
            spaceAfter=2, textColor=base["Heading3"].textColor,
        ),
        "muted": ParagraphStyle(
            "ReportMuted", parent=body, fontSize=9, textColor="#666666"
        ),
        "body": body,
    }


def _inline(text: str) -> str:
    """Escape text, then re-apply the inline Markdown as ReportLab markup."""
    out = html.escape(text)
    for pattern, repl in _INLINE:
        out = pattern.sub(repl, out)
    return out


def _para(text: str, style: ParagraphStyle) -> Paragraph:
    """Build a Paragraph from inline Markdown, falling back on bad markup.

    The model can emit text that, once converted to ReportLab's mini-HTML,
    nests tags in a way its strict parser rejects (and raises on). Rather than
    let one stray span fail the whole download, we retry with the markup
    stripped to plain escaped text, the report still renders, just without that
    fragment's emphasis.
    """
    try:
        return Paragraph(_inline(text), style)
    except Exception:
        return Paragraph(html.escape(text), style)


def _checklist_line(action: str, limit: int = 120) -> str:
    """Shorten one checklist entry to a line that can be scanned down a page.

    An entry arrives as "[code] <the rule's full sentence>", and that sentence
    is already printed in full under its document. A checklist is read by
    running an eye down the left edge, so it keeps the code (which points at the
    detail) and the opening of the rule, cut at a word boundary rather than
    mid-word. Anything already short is left alone.
    """
    action = " ".join(action.split())
    if len(action) <= limit:
        return action
    head = action[:limit].rsplit(" ", 1)[0].rstrip(" ,;:")
    return f"{head}…"


def _markdown_flowables(md: str, styles: dict) -> list:
    """Convert one Markdown block to flowables.

    Args:
        md: The stored Markdown of one report block.
        styles: The paragraph styles to draw from.

    Returns:
        A flat list of flowables, ready to extend the story with.
    """
    flowables: list = []
    bullets: list[str] = []
    numbered: list[str] = []

    def flush() -> None:
        for items, bullet in ((bullets, "bullet"), (numbered, "1")):
            if not items:
                continue
            flowables.append(
                ListFlowable(
                    [ListItem(_para(i, styles["body"])) for i in items],
                    bulletType=bullet,
                    leftIndent=14,
                )
            )
            items.clear()

    for raw in (md or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        bullet = re.match(r"^\s*[-*+]\s+(.*)$", line)
        number = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if bullet:
            numbered and flush()
            # A marker with nothing after it, from a stray Enter in the editor
            # or a model emitting "- **", prints as an empty bullet or worse as
            # a literal "****" once the emphasis markers survive alone. There is
            # no item here, so there is no line to print.
            item = bullet.group(1).strip()
            if item.strip("*_` "):
                bullets.append(item)
        elif number:
            bullets and flush()
            item = number.group(1).strip()
            if item.strip("*_` "):
                numbered.append(item)
        elif heading:
            flush()
            level = len(heading.group(1))
            style = styles["h1"] if level <= 2 else styles["h2"]
            flowables.append(_para(heading.group(2), style))
        else:
            flush()
            flowables.append(_para(line, styles["body"]))
    flush()
    return flowables


def render_report_pdf(
    course: Course,
    report: CourseReport,
    checklist: list[str] | list[tuple[str, list[str]]] | None = None,
) -> bytes:
    """Build the course report PDF.

    Args:
        course: Course the report belongs to, for the title page.
        report: Report whose stored blocks to render.
        checklist: Actions to render as tick boxes, either grouped per document
            as report_priorities.checklist_by_document returns them, or as a
            flat list of lines.

    Returns:
        The PDF bytes.
    """
    styles = _styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title=f"{course.title}, course report",
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    created_at = getattr(report, "created_at", None)
    created = created_at.strftime("%d %b %Y") if created_at else ""
    story: list = [
        _para(course.title or "Course report", styles["title"]),
        Paragraph(f"Course report · {created}" if created else "Course report",
                  styles["muted"]),
        Spacer(1, 8),
    ]

    # Lead with the headline diagnostics, the same numbers as the web gauges, so
    # the PDF opens with where the course stands, then the prioritised fixes.
    metrics = parse_metrics(getattr(report, "metrics_json", None))
    if metrics:
        story.append(Paragraph("Diagnostics", styles["h1"]))
        bits = [f"Course health: **{metrics['health_score']}/100** "
                f"({metrics['passed']}/{metrics['checked']} checks passed)"]
        if metrics.get("ai_readiness_score") is not None:
            bits.append(f"AI-readiness: **{metrics['ai_readiness_score']}/100**")
        if metrics.get("high_severity_failures"):
            bits.append(f"High-severity issues: **{metrics['high_severity_failures']}**")
        story.append(_para("  ·  ".join(bits), styles["body"]))

    # The document reads in three movements, each saying its thing once: where
    # the course stands, then what to do first, then the evidence document by
    # document for anyone who wants the detail. The action checklist used to sit
    # in the middle restating every failed check in full, so the same finding
    # was read four times over: as a priority, a checklist line, an issue and a
    # suggestion. It is now a compact tick-list of rule codes, with the wording
    # living once in the per-document detail it belongs to.
    if report.summary_md:
        story.append(Paragraph("Overall", styles["h1"]))
        story.extend(_markdown_flowables(report.summary_md, styles))
    if getattr(report, "alignment_md", None):
        story.append(Paragraph("Constructive alignment", styles["h1"]))
        story.extend(_markdown_flowables(report.alignment_md, styles))

    if report.priorities_md:
        story.append(Paragraph("Fix these first", styles["h1"]))
        story.append(
            Paragraph(
                "The highest-severity findings, in the order worth addressing "
                "them. Each one is explained in full under its document below.",
                styles["muted"],
            )
        )
        story.extend(_markdown_flowables(report.priorities_md, styles))
    if report.solutions_md:
        story.append(Paragraph("Course-level suggestions", styles["h1"]))
        story.append(
            Paragraph(
                "Changes that apply across the course rather than to one file.",
                styles["muted"],
            )
        )
        story.extend(_markdown_flowables(report.solutions_md, styles))

    # The evidence, one file at a time: what was found, then what to do about it.
    if report.sections:
        story.append(PageBreak())
        story.append(Paragraph("Findings by document", styles["h1"]))
        story.append(
            Paragraph(
                "Every issue the checks raised, with the suggested improvement "
                "beside it.",
                styles["muted"],
            )
        )
    for section in report.sections:
        story.append(_para(section.filename, styles["h2"]))
        story.append(Paragraph("Issues", styles["label"]))
        story.extend(
            _markdown_flowables(section.issues_md or "*No issues.*", styles)
        )
        if (section.solutions_md or "").strip():
            story.append(Paragraph("Suggested improvements", styles["label"]))
            story.extend(_markdown_flowables(section.solutions_md, styles))

    if metrics and metrics.get("never_applicable"):
        story.append(Paragraph("Checks that didn't apply", styles["h1"]))
        story.append(
            Paragraph(
                "These rules look for a situation the materials don't currently "
                "contain, so there was nothing to judge: they were neither "
                "passed nor failed and don't count in the score.",
                styles["muted"],
            )
        )
        na_md = "\n".join(
            f"- {'**[' + n['code'] + ']** ' if n.get('code') else ''}{n['rule']}"
            for n in metrics["never_applicable"]
        )
        story.extend(_markdown_flowables(na_md, styles))

    # The checklist closes the report as a page to work from: one tickable line
    # per failed check, identified by its code and short rule name, not the full
    # paragraph already printed above.
    if checklist:
        story.append(PageBreak())
        story.append(Paragraph("Action checklist", styles["h1"]))
        story.append(
            Paragraph(
                "One line per failed check, to tick off as you address it. The "
                "full explanation of each is under its document above.",
                styles["muted"],
            )
        )
        # Grouped per document so the professor works through one file at a
        # time; a plain list of strings is still accepted.
        groups = (
            checklist
            if checklist and isinstance(checklist[0], tuple)
            else [(None, list(checklist))]
        )
        for filename, actions in groups:
            if filename:
                story.append(_para(filename, styles["label"]))
            story.append(
                ListFlowable(
                    [
                        ListItem(_para(_checklist_line(a), styles["body"]))
                        for a in actions
                    ],
                    bulletType="bullet",
                    bulletChar="☐",
                    leftIndent=14,
                )
            )

    doc.build(story)
    return buffer.getvalue()
