"""Deterministic triage of a run's failures: priority order and effort class.

Pure functions over the report view's verdicts, no model call, no writes. Two
products, both derived only from each failed rule's stored severity and category
(app/models/rule.py).

The priority order sorts failures by severity weight, highest first, which is
the order the report and the PDF lead with. The effort class marks each failure
a quick win or a structural change: a quick win is a low-effort polish fix such
as a typo, a missing weight or an off-goal slide, low or medium severity in the
clarity and content themes. Everything else is structural, asking the professor
to redesign part of the course rather than edit a line.

The split is coarse and deterministic so it cannot drift between runs; the model
only phrases the advice (report_generation), never decides what is urgent.
"""

from dataclasses import dataclass

from app.models import RuleCategory, RuleSeverity
from app.services.report_view import ReportView, RuleVerdict

QUICK_WIN = "quick_win"
STRUCTURAL = "structural"

# Themes whose fixes are usually a quick polish rather than a redesign.
_QUICK_CATEGORIES = (RuleCategory.CLARITY, RuleCategory.CONTENT)


def effort_class(verdict: RuleVerdict) -> str:
    """Classify how much work a failure asks for.

    Args:
        verdict: The failed verdict to classify.

    Returns:
        QUICK_WIN for a low or medium polish fix, STRUCTURAL otherwise.
    """
    if verdict.severity == RuleSeverity.HIGH:
        return STRUCTURAL
    if verdict.rule.category in _QUICK_CATEGORIES:
        return QUICK_WIN
    return STRUCTURAL


@dataclass
class PriorityItem:
    """One failed check, ready for the priority list and the model context.

    Attributes:
        quotes: The {quote, page} entries grounding the failure.
        code: The failed rule's stable code, shown in the "fix these first"
            bullets so they name the same check as the issues and the Rules
            page.
    """

    filename: str
    document_type: str
    rule_text: str
    rationale: str | None
    category: str
    severity: str
    effort: str
    quotes: list[dict]
    code: str | None = None


def _weight(item: PriorityItem) -> int:
    """Sort key: the item's severity weight."""
    return RuleSeverity.weight(item.severity)


def collect_failures(view: ReportView) -> list[PriorityItem]:
    """Every failed verdict across the course, most severe first.

    Args:
        view: Report view to read the verdicts from.

    Returns:
        The failures as priority items.
    """
    items: list[PriorityItem] = []
    for doc in view.documents:
        for v in doc.verdicts:
            if v.outcome != "fail":
                continue
            quotes = [q for f in v.facts for q in f.quotes if q.get("quote")]
            items.append(
                PriorityItem(
                    filename=doc.filename,
                    document_type=doc.document_type,
                    rule_text=v.rule.text,
                    rationale=v.rule.rationale,
                    category=v.rule.category,
                    severity=v.severity,
                    effort=effort_class(v),
                    quotes=quotes,
                    code=v.rule.code,
                )
            )
    items.sort(key=_weight, reverse=True)
    return items


def top_priorities(view: ReportView, limit: int = 5) -> list[PriorityItem]:
    """The most severe failures, for the lead "fix these first" block.

    Args:
        view: Report view to read the verdicts from.
        limit: How many failures to keep.

    Returns:
        The top failures, most severe first.
    """
    return collect_failures(view)[:limit]


def action_checklist(view: ReportView) -> list[str]:
    """One tick-box line per failed check, most severe first, for the PDF.

    Args:
        view: Report view to read the verdicts from.

    Returns:
        One line per failure, each naming its rule code and document.
    """
    return [
        f"{'[' + p.code + '] ' if p.code else ''}[{p.filename}] {p.rule_text}"
        for p in collect_failures(view)
    ]


def checklist_by_document(view: ReportView) -> list[tuple[str, list[str]]]:
    """The action checklist grouped per document.

    Same failures and order as action_checklist, gathered per file: a flat list
    interleaves documents, since a course-wide rule fails on several files.

    Args:
        view: Report view to read the verdicts from.

    Returns:
        One (filename, actions) pair per document, ordered by each document's
        most severe failure.
    """
    order: list[str] = []
    grouped: dict[str, list[str]] = {}
    for p in collect_failures(view):  # already sorted most severe first
        if p.filename not in grouped:
            grouped[p.filename] = []
            order.append(p.filename)
        label = f"{'[' + p.code + '] ' if p.code else ''}{p.rule_text}"
        grouped[p.filename].append(label)
    return [(name, grouped[name]) for name in order]
