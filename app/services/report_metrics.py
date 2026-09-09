"""Chart data for the course report.

Every number here is deterministic, computed purely from the run's rule verdicts
and the rules' declared severity and category (app/models/rule.py), so the same
verdicts always give the same chart and no gauge can contradict its bar. This
includes the AI-readiness score, the severity-weighted pass share of the
ai_readiness-category rules alone. The only model-produced field is
ai_readiness_note, a short narrative the report generator writes from those same
verdicts to explain the score; it is filled in later and is never a number.

The result is serialised onto CourseReport.metrics_json by report generation and
rendered by courses/report_view.html, so charts never drift between renders.
"""

import json
import logging
from dataclasses import asdict, dataclass, field

from app.models import RuleCategory, RuleSeverity
from app.services.report_view import ReportView

logger = logging.getLogger(__name__)


@dataclass
class CategoryBar:
    """One improvement area's pass and fail counts, for the radar and the bars."""

    category: str
    passed: int
    failed: int

    @property
    def checked(self) -> int:
        """How many checks ran in this area."""
        return self.passed + self.failed

    @property
    def pass_pct(self) -> int:
        """Share of this area's checks that passed, 0 when none ran."""
        return round(100 * self.passed / self.checked) if self.checked else 0


@dataclass
class ReportMetrics:
    """One run's chart data, serialised onto CourseReport.metrics_json.

    Attributes:
        health_score: Severity-weighted share of passing checks, 0 to 100.
        failures_by_severity: Failure counts split by severity, for the bars.
        ai_readiness_score: Severity-weighted pass share of the
            ai_readiness-category rules alone, so the gauge can never contradict
            its own bar. None when the course has no such rule checked.
        ai_readiness_note: Short narrative the model writes from those verdicts;
            filled in by report generation, after this is built.
        never_applicable: Conditional rules that never found their subject in
            any document they ran on, as {rule, documents, missing}. Excluded
            from every count above and rendered as their own report block, never
            mixed into the summary.
    """

    health_score: int = 0
    passed: int = 0
    checked: int = 0
    categories: list[CategoryBar] = field(default_factory=list)
    high_severity_failures: int = 0
    failures_by_severity: dict[str, int] = field(
        default_factory=lambda: {"high": 0, "medium": 0, "low": 0}
    )
    ai_readiness_score: int | None = None
    ai_readiness_passed: int = 0
    ai_readiness_checked: int = 0
    ai_readiness_note: str | None = None
    never_applicable: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        """Serialise for storage, expanding each bar's derived properties."""
        data = asdict(self)
        data["categories"] = [
            {**asdict(c), "checked": c.checked, "pass_pct": c.pass_pct}
            for c in self.categories
        ]
        return json.dumps(data)


def parse_metrics(metrics_json: str | None) -> dict | None:
    """Decode a report's stored metrics for the template.

    Args:
        metrics_json: The stored JSON, or None.

    Returns:
        The metrics dict, or None when absent or unreadable.
    """
    if not metrics_json:
        return None
    try:
        return json.loads(metrics_json)
    except (ValueError, TypeError):
        return None


def build_metrics(view: ReportView) -> ReportMetrics:
    """Build the deterministic chart data from a report view's verdicts.

    Args:
        view: Report view holding the run's verdicts.

    Returns:
        The metrics, with ai_readiness_note left for report generation to fill.
    """
    cats: dict[str, CategoryBar] = {c: CategoryBar(c, 0, 0) for c in RuleCategory.ALL}
    weight_total = weight_passed = high_fail = passed = checked = 0
    fail_sev = {"high": 0, "medium": 0, "low": 0}
    # AI-readiness is scored from its own rules only, so its gauge agrees with
    # its bar: an ai_readiness rule passing can never drag the gauge down.
    ai_w_total = ai_w_passed = 0
    for doc in view.documents:
        for v in doc.verdicts:
            if v.outcome not in ("pass", "fail"):
                continue
            checked += 1
            severity = v.severity  # softened for assessment rules on slides
            weight = RuleSeverity.weight(severity)
            weight_total += weight
            bar = cats.setdefault(v.rule.category, CategoryBar(v.rule.category, 0, 0))
            is_ai = v.rule.category == RuleCategory.AI_READINESS
            if is_ai:
                ai_w_total += weight
            if v.outcome == "pass":
                passed += 1
                weight_passed += weight
                bar.passed += 1
                if is_ai:
                    ai_w_passed += weight
            else:
                bar.failed += 1
                fail_sev[severity] = fail_sev.get(severity, 0) + 1
                if severity == RuleSeverity.HIGH:
                    high_fail += 1
    ai_bar = cats[RuleCategory.AI_READINESS]
    return ReportMetrics(
        health_score=round(100 * weight_passed / weight_total) if weight_total else 0,
        passed=passed,
        checked=checked,
        categories=[c for c in cats.values() if c.checked],
        high_severity_failures=high_fail,
        failures_by_severity=fail_sev,
        ai_readiness_score=round(100 * ai_w_passed / ai_w_total) if ai_w_total else None,
        ai_readiness_passed=ai_bar.passed,
        ai_readiness_checked=ai_bar.checked,
        never_applicable=getattr(view, "never_applicable_rules", lambda: [])(),
    )
