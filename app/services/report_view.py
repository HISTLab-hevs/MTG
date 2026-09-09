"""Read model for the Report page.

Assembles the persisted RuleEvaluation rows (app.services.analysis) into a
per-document view the template renders: each document carries its latest
version's verdicts grouped by outcome, plus course-wide totals. A pure read,
with no model call and no writes.

The grounded rationale (rule_rationale) turns one evaluation's stored activation
into the immutable what, where and why: each contract field, its aggregated
value, and the verbatim source quotes with their pages that justify it. This
same structure feeds both this view and the report model, so the report is
grounded in real source spans and the model never invents issues.
"""

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import (
    Course,
    CourseReport,
    CourseRule,
    DocumentStatus,
    DocumentType,
    EvaluationStatus,
    ReportRun,
    ReportStatus,
    RuleCategory,
    RuleSeverity,
    RuleEvaluation,
)
from app.services import contracts

# An assessment-domain rule (a formal-grading or AI-integrity check) is written
# for real assessments. When the document is also a slide deck, a lecture with
# a small homework at the end, holding that homework to exam-grade rigour
# buries the professor in high-severity failures. The gap is still reported, one
# severity step lower. Pure slides and real assessments are untouched.
_SOFTENED_CATEGORIES = (RuleCategory.ASSESSMENT, RuleCategory.AI_READINESS)
_SEVERITY_DOWN = {
    RuleSeverity.HIGH: RuleSeverity.MEDIUM,
    RuleSeverity.MEDIUM: RuleSeverity.LOW,
    RuleSeverity.LOW: RuleSeverity.LOW,
}


def effective_severity(rule: CourseRule, document_type: str) -> str:
    """The rule's severity as it should count for one particular document.

    Downgraded one step when an assessment-domain rule, scoped to assessments or
    rubrics, lands on a document that is also a slide deck. Everywhere else it
    is the rule's own severity.

    Args:
        rule: Rule being evaluated.
        document_type: The document's stored types.

    Returns:
        The severity to count, deterministically.
    """
    types = DocumentType.parse(document_type)
    rule_types = DocumentType.parse(rule.document_type)
    if (
        DocumentType.SLIDE in types
        and rule.category in _SOFTENED_CATEGORIES
        and set(rule_types) <= {DocumentType.ASSESSMENT, DocumentType.RUBRIC}
        and rule_types
    ):
        return _SEVERITY_DOWN.get(rule.severity, rule.severity)
    return rule.severity


def _humanise_field(name: str) -> str:
    """Turn a snake_case contract field into a short readable label.

    For instance reasoning_question_count becomes "Reasoning question count".
    """
    words = name.replace("_", " ").split()
    out = []
    for w in words:
        if w in ("ai",):
            out.append("AI")
        else:
            out.append(w)
    label = " ".join(out)
    return label[:1].upper() + label[1:] if label else name


def _display_value(value: object) -> str:
    """Render an extracted value the way a professor reads it."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict) and {"item", "page"} <= value.keys():
        # A tagged_pages entry: "kinetic energy (p. 3)".
        return f"{value['item']} (p. {value['page']})"
    if isinstance(value, (list, tuple)):
        return ", ".join(_display_value(v) for v in value) if value else "none"
    if value is None:
        return "not found"
    return str(value)


@dataclass
class RuleFact:
    """One contract field's aggregated value and its grounding quotes.

    Attributes:
        quotes: The {quote, page} spans justifying the value.
        gate: Whether the field is one of the rule's preconditions.
    """

    field: str
    value: object
    quotes: list[dict]
    gate: bool = False

    @property
    def label(self) -> str:
        """Readable label for the field, derived from its name."""
        return _humanise_field(self.field)

    @property
    def value_display(self) -> str:
        """The aggregated value, rendered the way a professor reads it."""
        return _display_value(self.value)

    @property
    def value_display_items(self) -> list[str]:
        """A list value as readable per-item lines, with duplicates grouped.

        Extractors that tag pages return one {item, page} entry per occurrence,
        so a concept spanning eight pages appears eight times, which
        value_display would join into a wall of text. An item's occurrences
        collapse here to one line with its pages merged, as in "safety property
        (p. 13, 14, 15)". Empty for scalar values, where the template falls back
        to value_display.
        """
        if not isinstance(self.value, (list, tuple)) or not self.value:
            return []
        grouped: dict[str, list] = {}
        for v in self.value:
            if isinstance(v, dict) and {"item", "page"} <= v.keys():
                grouped.setdefault(str(v["item"]), []).append(v["page"])
            else:
                grouped.setdefault(_display_value(v), [])
        out = []
        for item, pages in grouped.items():
            shown = sorted({p for p in pages if p is not None})
            out.append(f"{item} (p. {', '.join(map(str, shown))})" if shown else item)
        return out

    @property
    def is_guard(self) -> bool:
        """Whether this is a precondition field rather than the verdict's
        substance, carried for traceability."""
        return self.gate

    @property
    def is_informative(self) -> bool:
        """Whether the fact is worth showing the professor as its own line.

        It must carry a value and not be a merely satisfied guard. A false
        guard, the reason a rule passed vacuously, is still informative; a true
        one is noise.
        """
        if self.value is None:
            return False
        if self.is_guard and self.value is True:
            return False
        return True


def rule_rationale(rule: CourseRule, activation_json: str | None) -> list[RuleFact]:
    """The grounded facts a verdict ran over.

    Pairs each of the rule's contract fields with its aggregated value and the
    source spans that justify it. Deterministic, no model call.

    Args:
        rule: Rule whose contract to read.
        activation_json: The verdict's stored activation.

    Returns:
        One fact per contract field; empty when the activation is absent or
        unreadable.
    """
    if not activation_json:
        return []
    try:
        data = json.loads(activation_json)
    except (ValueError, TypeError):
        return []
    doc = data.get("doc", {}) if isinstance(data, dict) else {}
    evidence = data.get("evidence", {}) if isinstance(data, dict) else {}
    facts = []
    for f in contracts.contract_fields(rule):
        facts.append(
            RuleFact(
                field=f.name,
                value=doc.get(f.name),
                quotes=evidence.get(f.name, []) or [],
                gate=f.gate,
            )
        )
    return facts


@dataclass
class RuleVerdict:
    """One rule's verdict on one document, with the facts behind it.

    Attributes:
        document_type: The types of the document this verdict is for, so the
            severity can be softened when an assessment rule lands on a slide
            deck. See effective_severity.
    """

    rule: CourseRule
    status: str
    value: bool | None
    error_message: str | None
    facts: list[RuleFact] = field(default_factory=list)
    document_type: str = ""

    @property
    def severity(self) -> str:
        """Severity as it counts for this document, softened on slides."""
        return effective_severity(self.rule, self.document_type)

    @property
    def is_not_applicable(self) -> bool:
        """Whether the rule's precondition never occurred in the document.

        The rule declared gate fields, its "if" part, and every one of them
        aggregated to a falsy value: no assessment task, no AI mention and so
        on. The CEL then passes vacuously, which is honest logic but a
        misleading verdict, since nothing was actually checked. It is reported
        as its own outcome so a professor can tell "satisfied" from "never
        triggered", and excluded from the health score, which counts passes and
        failures only.

        Three safeguards keep this from touching a rule that genuinely judged
        the document. A failure is never converted: if a rule fails, the failure
        stands regardless of its gates. A rule with no gate fields is never
        converted. And neither is a rule whose contract is only gate fields,
        since its gate is its verdict, as in "slides must not use AI", so a
        falsy gate there is a real pass rather than an absence of subject.

        Derived at view time from the stored activation, so it applies to old
        runs too.
        """
        if self.status != EvaluationStatus.EVALUATED or not self.value:
            return False
        gates = [f for f in self.facts if f.is_guard]
        substance = [f for f in self.facts if not f.is_guard]
        return bool(gates) and bool(substance) and all(not g.value for g in gates)

    @property
    def outcome(self) -> str:
        """The verdict as the template reads it: pass, fail, na, or the
        evaluation's own failure status."""
        if self.status == EvaluationStatus.EVALUATED:
            if self.is_not_applicable:
                return "na"
            return "pass" if self.value else "fail"
        return self.status  # skipped | failed

    @property
    def has_quotes(self) -> bool:
        """Whether the extractor grounded this verdict in any source span.

        True for both passes and failures whenever a quote exists: presence
        rules cite the passage that satisfies them, violation findings cite the
        offending span. When no field earned a quote, as for a violation rule
        that passed by finding nothing, there is simply nothing to show.
        """
        return any(f.quotes for f in self.facts)

    @property
    def informative_facts(self) -> list["RuleFact"]:
        """The facts worth showing the professor.

        The values that actually decided the verdict, counts, levels and lists,
        with satisfied guard preconditions filtered out.
        """
        return [f for f in self.facts if f.is_informative]

    @property
    def evidence_quotes(self) -> list[dict]:
        """The source spans to display, deduplicated.

        A guard field such as has_assessment_task only proves a task exists, so
        when any non-guard field carries its own quote, the guard's is dropped
        as noise. Guards are kept when they are the only thing with a quote, so
        the evidence is never empty while a span does exist.
        """
        non_guard = [f for f in self.facts if not f.is_guard and f.quotes]
        source = non_guard if non_guard else [f for f in self.facts if f.quotes]
        seen, out = set(), []
        for f in source:
            for q in f.quotes:
                key = (q.get("quote"), q.get("page"))
                if q.get("quote") and key not in seen:
                    seen.add(key)
                    out.append(q)
        return out

    @property
    def has_evidence_quotes(self) -> bool:
        """Whether there is any source span left to display."""
        return bool(self.evidence_quotes)

    @property
    def evidence_caption(self) -> str:
        """A line describing the evidence, so no verdict shows a bare quote box.

        A quote can be absent for several honest reasons, not only because
        something is missing, so the caption is derived from the outcome and
        from whether a quote exists, and never invented by the model. A pass
        with a quote names the passage that satisfies the rule and one without
        says the check passed with nothing to flag; a failure with a quote names
        the span where the rule is not met and one without says the required
        thing is absent from the document.
        """
        if self.outcome == "pass":
            return (
                "What satisfied the rule"
                if self.has_quotes
                else "This check passed, nothing to flag in the document."
            )
        if self.outcome == "fail":
            return (
                "Where the rule was not met"
                if self.has_quotes
                # A missing span has two causes that cannot be told apart
                # deterministically: the element is absent, or the rule is a
                # document-wide judgement such as "must be stimulating" with no
                # passage to point to. Naming both avoids reporting a quality
                # shortfall as a missing element.
                else "No passage to cite, what the rule requires is either "
                "absent from this document, or falls short document-wide "
                "rather than in one spot."
            )
        if self.outcome == "na":
            return (
                "Not applicable, the situation this rule checks for does not "
                "occur in this document."
            )
        return "No evidence for this check."


@dataclass
class DocumentReport:
    """One analysed document and every verdict its rules reached."""

    document_id: int
    filename: str
    document_type: str
    document_version_id: int | None = None
    version_number: int | None = None
    verdicts: list[RuleVerdict] = field(default_factory=list)

    @property
    def passed(self) -> int:
        """How many of the document's checks passed."""
        return sum(1 for v in self.verdicts if v.outcome == "pass")

    @property
    def failed(self) -> int:
        """How many of the document's checks failed."""
        return sum(1 for v in self.verdicts if v.outcome == "fail")

    @property
    def checked(self) -> int:
        """How many checks really judged the document.

        Not-applicable ones are a non-event, so "3/5 passed" never counts a
        check that never triggered.
        """
        return sum(1 for v in self.verdicts if v.outcome in ("pass", "fail"))

    @property
    def not_applicable(self) -> int:
        """How many checks never found their subject in the document."""
        return sum(1 for v in self.verdicts if v.outcome == "na")


@dataclass
class DocCategoryCell:
    """One cell of the summary grid: one document's standing in one category.

    Attributes:
        state: One of "pass", "fail", "partial" or "na".
    """

    state: str
    passed: int
    failed: int


@dataclass
class SummaryRow:
    """One document's row of the summary grid."""

    filename: str
    document_type: str
    cells: list[DocCategoryCell]
    failed: int


@dataclass
class SummaryGrid:
    """The document-by-category matrix the report renders as a table."""

    categories: list[str]
    rows: list[SummaryRow]


@dataclass
class ReportView:
    """One run's whole read model: every analysed document with its verdicts."""

    documents: list[DocumentReport] = field(default_factory=list)

    @property
    def has_results(self) -> bool:
        """Whether any document was actually checked."""
        return any(d.verdicts for d in self.documents)

    def never_applicable_rules(self) -> list[dict]:
        """The rules that were not applicable on every document they ran on.

        Says what a per-document list cannot: the course as a whole never
        presented the situation the rule checks for, with no graded assessment
        content or AI never addressed.

        Returns:
            One entry per such rule, carrying its text, how many documents it
            was evaluated against, and readable labels of the absent gate
            subjects, so the report can name what was never found.
        """
        by_rule: dict[int, dict] = {}
        for doc in self.documents:
            for v in doc.verdicts:
                if v.outcome not in ("pass", "fail", "na"):
                    continue
                entry = by_rule.setdefault(
                    v.rule.id,
                    {
                        "code": v.rule.code,
                        "rule": v.rule.text,
                        "documents": 0,
                        "na": 0,
                        "missing": [],
                    },
                )
                entry["documents"] += 1
                if v.outcome == "na":
                    entry["na"] += 1
                    entry["missing"] = [
                        f.label for f in v.facts if f.is_guard and not f.value
                    ]
        return [
            {
                "code": e["code"],
                "rule": e["rule"],
                "documents": e["documents"],
                "missing": e["missing"],
            }
            for e in by_rule.values()
            if e["documents"] and e["na"] == e["documents"]
        ]

    def summary_grid(self) -> SummaryGrid:
        """Build the document-by-category grid.

        Each cell collapses a document's verdicts for one category: all passing
        is a pass, all failing a fail, a mix partial, and nothing checked not
        applicable.

        Returns:
            The grid, over the categories in play only: those with no checked
            rule anywhere are dropped so the table stays compact.
        """
        active = [
            c
            for c in RuleCategory.ALL
            if any(
                v.rule.category == c and v.outcome in ("pass", "fail")
                for d in self.documents
                for v in d.verdicts
            )
        ]
        rows: list[SummaryRow] = []
        for doc in self.documents:
            if not doc.verdicts:
                continue
            cells = []
            for c in active:
                p = sum(
                    1 for v in doc.verdicts
                    if v.rule.category == c and v.outcome == "pass"
                )
                f = sum(
                    1 for v in doc.verdicts
                    if v.rule.category == c and v.outcome == "fail"
                )
                if p == 0 and f == 0:
                    state = "na"
                elif f == 0:
                    state = "pass"
                elif p == 0:
                    state = "fail"
                else:
                    state = "partial"
                cells.append(DocCategoryCell(state=state, passed=p, failed=f))
            rows.append(
                SummaryRow(
                    filename=doc.filename,
                    document_type=doc.document_type,
                    cells=cells,
                    failed=doc.failed,
                )
            )
        return SummaryGrid(categories=active, rows=rows)


def document_markdown(document) -> str:
    """The full extracted Markdown of a document's latest ready version.

    Args:
        document: Document to read.

    Returns:
        Its pages in order, joined; empty when it never finished processing.
    """
    version = document.latest_version
    if version is None or version.status != DocumentStatus.READY:
        return ""
    return "\n\n".join(
        page.extraction.markdown
        for page in version.pages
        if page.extraction and page.extraction.markdown
    )


def build_report_view(db: Session, course: Course) -> ReportView:
    view = ReportView()
    for document in course.documents:
        version = document.latest_version
        report = DocumentReport(
            document_id=document.id,
            filename=document.filename,
            document_type=document.document_type,
            document_version_id=version.id if version is not None else None,
            version_number=version.version_number if version is not None else None,
        )
        if version is not None and version.status == DocumentStatus.READY:
            rows = db.execute(
                select(RuleEvaluation)
                .where(RuleEvaluation.document_version_id == version.id)
                .options(joinedload(RuleEvaluation.rule))  # avoid a per-row rule lazy-load
                .order_by(RuleEvaluation.id)
            ).scalars().all()
            report.verdicts = [
                RuleVerdict(
                    rule=row.rule,
                    status=row.status,
                    value=row.value,
                    error_message=row.error_message,
                    facts=rule_rationale(row.rule, row.activation),
                    document_type=document.document_type,
                )
                for row in rows
            ]
        view.documents.append(report)
    return view


def latest_course_report(db: Session, course: Course) -> CourseReport | None:
    """The course's most recent completed report, or None when it has none."""
    reports = completed_course_reports(db, course, limit=1)
    return reports[0] if reports else None


def completed_course_reports(
    db: Session, course: Course, limit: int | None = None
) -> list[CourseReport]:
    """The course's completed reports, newest first, as the history list.

    Args:
        db: Session to query.
        course: Course whose reports to list.
        limit: How many to return; all of them when omitted.

    Returns:
        The reports, newest first.
    """
    stmt = (
        select(CourseReport)
        .join(ReportRun, CourseReport.report_run_id == ReportRun.id)
        .where(
            ReportRun.course_id == course.id,
            CourseReport.status == ReportStatus.COMPLETED,
        )
        .order_by(CourseReport.created_at.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.execute(stmt).scalars())
