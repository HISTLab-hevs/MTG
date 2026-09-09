"""Rebuild a past run's read model from the snapshot frozen on its report.

The Report page normally reads live RuleEvaluation rows (report_view), but those
are keyed by document version and rule, and are overwritten by the next
analysis: an older run's verdicts no longer exist as rows. What does survive is
CourseReport.snapshot_json (report_history.build_snapshot): the verdicts, their
grounding facts and quotes, and the frozen rule definitions, exactly as that run
saw them.

So opening an archived report reconstructs its ReportView from that snapshot
rather than from current rows. The rebuilt view feeds the same templates,
summary grid and PDF as a live one, which is why the pieces are rebuilt as
objects carrying the attributes those consumers read.

One honest gap: build_snapshot stores only pass/fail verdicts, so a rebuilt view
has no not-applicable or skipped entries. Callers surface that rather than
implying the older run had none.
"""

import json
from dataclasses import dataclass, field

from app.models import CourseReport, EvaluationStatus
from app.services.report_view import DocumentReport, ReportView, RuleFact, RuleVerdict


@dataclass
class FrozenRule:
    """A rule as one past run defined it, standing in for a CourseRule.

    Only the attributes the report views and metrics actually read are carried.

    Attributes:
        id: Synthesised, because the snapshot does not store live rule ids and
            must not: the live rule may since have been edited or deleted. It
            only has to be stable and unique within the rebuilt view, which is
            all its consumers use it for.
    """

    id: int
    code: str | None
    text: str
    rationale: str | None
    cel: str | None
    extraction_contract: str | None
    severity: str
    category: str
    document_type: str


def _frozen_rules(data: dict) -> dict[str, FrozenRule]:
    """The run's frozen rule definitions.

    Args:
        data: Decoded snapshot.

    Returns:
        The rules keyed by code, falling back to their text.
    """
    rules: dict[str, FrozenRule] = {}
    for index, entry in enumerate(data.get("rules") or [], start=1):
        if not isinstance(entry, dict):
            continue
        key = entry.get("code") or entry.get("text") or str(index)
        rules[key] = FrozenRule(
            id=index,
            code=entry.get("code"),
            text=entry.get("text") or "",
            rationale=entry.get("rationale"),
            cel=entry.get("cel"),
            extraction_contract=entry.get("extraction_contract"),
            severity=entry.get("severity") or "",
            category=entry.get("category") or "",
            document_type=entry.get("document_type") or "",
        )
    return rules


def _verdict_rule(verdict: dict, rules: dict[str, FrozenRule]) -> FrozenRule:
    """The frozen definition behind one snapshot verdict.

    Normally a lookup by code. A snapshot written before rules carried codes, or
    one whose rule entry is missing, still has the verdict's own text, severity
    and category, enough to render the row, so it is rebuilt from those and
    memoised under the same key. That keeps one synthetic rule, and one id, per
    distinct rule rather than one per verdict.

    Args:
        verdict: One snapshot verdict.
        rules: The frozen rules, extended in place when one is synthesised.

    Returns:
        The rule behind the verdict.
    """
    key = verdict.get("code") or verdict.get("rule") or ""
    found = rules.get(key)
    if found is not None:
        return found
    rules[key] = FrozenRule(
        id=len(rules) + 1,
        code=verdict.get("code"),
        text=verdict.get("rule") or "",
        rationale=None,
        cel=None,
        extraction_contract=None,
        severity=verdict.get("severity") or "",
        category=verdict.get("category") or "",
        document_type="",
    )
    return rules[key]


def rebuild_view(report: CourseReport) -> ReportView:
    """Rebuild the view of the run that produced a report, from its snapshot.

    Args:
        report: Archived report to rebuild.

    Returns:
        The view as that run saw it. Empty when the report predates snapshots
        or its JSON is unreadable; callers treat that as "nothing to show for
        this run" rather than falling back to current rows, which would silently
        show another run's results under an old report's date.
    """
    view = ReportView()
    if not report.snapshot_json:
        return view
    try:
        data = json.loads(report.snapshot_json)
    except (ValueError, TypeError):
        return view
    if not isinstance(data, dict):
        return view

    rules = _frozen_rules(data)
    for doc in data.get("documents") or []:
        if not isinstance(doc, dict):
            continue
        document = DocumentReport(
            document_id=doc.get("document_id"),
            filename=doc.get("filename") or "",
            document_type=doc.get("document_type") or "",
            document_version_id=doc.get("document_version_id"),
            version_number=doc.get("version_number"),
        )
        for entry in doc.get("verdicts") or []:
            if not isinstance(entry, dict):
                continue
            rule = _verdict_rule(entry, rules)
            outcome = entry.get("outcome")
            document.verdicts.append(
                RuleVerdict(
                    rule=rule,
                    status=EvaluationStatus.EVALUATED,
                    value=(outcome == "pass"),
                    error_message=None,
                    facts=[
                        RuleFact(
                            field=f.get("field") or "",
                            value=f.get("value"),
                            quotes=list(f.get("quotes") or []),
                            gate=bool(f.get("gate")),
                        )
                        for f in entry.get("facts") or []
                        if isinstance(f, dict)
                    ],
                    document_type=doc.get("document_type") or "",
                )
            )
        view.documents.append(document)
    return view
