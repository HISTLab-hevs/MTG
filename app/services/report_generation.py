"""Write the course report from the deterministic rule verdicts.

Runs after analysis, chained at the tail of app.services.analysis.run_analysis.
The rule engine has already produced grounded verdicts; this stage only turns
them into prose, in two passes over data that is already deterministic.

Per document, the failed verdicts with their grounded field values and verbatim
quotes, plus the full document markdown, go to the model, which explains and
groups the issues and proposes fixes; the issues are immutable and the solutions
are the editable part (see app.models.course_report). Course-wide, those
per-document issues are then synthesised into one overall judgement plus general
advice.

When no generation endpoint is configured, as in development and tests, the
model calls fall back to a deterministic template so the whole flow and its
tests still run.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.db import SessionLocal
from app.llm.client import NO_THINKING, get_report_client
from app.llm.prompts import (
    char_budget,
    clip_markdown_to_budget,
    loads_model_json,
    render_prompt,
)
from app.models import (
    Course,
    CourseReport,
    Document,
    DocumentReportSection,
    DocumentStatus,
    DocumentType,
    DocumentVersion,
    Page,
    ReportRun,
    ReportStatus,
    RuleCategory,
)
from app.services.markdown_utils import clean_markdown
from app.services.report_history import build_snapshot
from app.services.report_metrics import ReportMetrics, build_metrics
from app.services.report_priorities import (
    QUICK_WIN,
    PriorityItem,
    top_priorities,
)
from app.services.report_view import RuleVerdict, build_report_view, document_markdown

logger = logging.getLogger(__name__)

# A report call can legitimately ask for a full max_tokens answer, and up to
# report_generation_concurrency of them share the vLLM server's concurrency
# budget, so one call can sit well past the client's default timeout without
# anything being wrong.
_REPORT_CALL_TIMEOUT = 300.0


def _client():
    """The report-generation model client, with headroom for a long answer."""
    client = get_report_client()
    if client.is_configured:
        client.request_timeout = max(client.request_timeout, _REPORT_CALL_TIMEOUT)
    return client

def _doc_schema(finding_count: int) -> dict:
    """The constrained shape for one document's report.

    One issue string and one solution string per failed rule, with the arrays
    pinned to exactly that many entries. A single free-text issues field lets a
    small model latch onto the biggest finding, usually the typo list, and
    silently drop the others; pinning the count gives every failed rule its
    bullet by construction rather than by prompt obedience.

    Args:
        finding_count: How many rules failed on the document.

    Returns:
        The schema, passed to the model as guided_json.
    """
    per_rule = {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "minItems": finding_count,
        "maxItems": finding_count,
    }
    return {
        "type": "object",
        "properties": {"issues": per_rule, "solutions": per_rule},
        "required": ["issues", "solutions"],
        "additionalProperties": False,
    }
_COURSE_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "solutions": {"type": "string"}},
    "required": ["summary", "solutions"],
    "additionalProperties": False,
}
_READINESS_SCHEMA = {
    "type": "object",
    "properties": {"note": {"type": "string"}},
    "required": ["note"],
    "additionalProperties": False,
}
_PRIORITIES_SCHEMA = {
    "type": "object",
    "properties": {"priorities": {"type": "string"}},
    "required": ["priorities"],
    "additionalProperties": False,
}


def generate_report(report_run_id: int) -> None:
    """Build one CourseReport for a completed analysis run.

    Called at the tail of the analysis job, on the reports worker. Owns its own
    session and records the outcome on the report itself.

    Args:
        report_run_id: Run to report on; an unknown id is a no-op.
    """
    db = SessionLocal()
    run = db.get(ReportRun, report_run_id)
    if run is None:
        db.close()
        return
    report = db.execute(
        select(CourseReport).where(CourseReport.report_run_id == run.id)
    ).scalar_one_or_none()
    if report is not None and report.status == ReportStatus.COMPLETED:
        db.close()
        return
    if report is None:
        report = CourseReport(report_run_id=run.id, status=ReportStatus.RUNNING)
        db.add(report)
    else:
        report.status = ReportStatus.RUNNING
        report.sections.clear()
        for field in (
            "summary_md",
            "solutions_md",
            "solutions_md_prev",
            "priorities_md",
            "alignment_md",
            "metrics_json",
            "snapshot_json",
        ):
            setattr(report, field, None)
    db.commit()
    try:
        course = db.execute(
            select(Course)
            .where(Course.id == run.course_id)
            .options(
                selectinload(Course.documents)
                .selectinload(Document.versions)
                .selectinload(DocumentVersion.pages)
                .selectinload(Page.extraction)
            )
        ).scalar_one()
        _build(db, course, report, run)
        report.status = ReportStatus.COMPLETED
        db.commit()
    except Exception:
        logger.exception("Report generation failed for run %s", report_run_id)
        db.rollback()
        report.status = ReportStatus.FAILED
        db.commit()
        raise
    finally:
        db.close()


def _build(
    db: Session, course: Course, report: CourseReport, run: ReportRun | None = None
) -> None:
    view = build_report_view(db, course)
    markdowns = _document_markdowns(course)

    def heartbeat() -> None:
        # The report phase makes one model call per failing document plus a few
        # course-level calls, each of which can take a while. Without a heartbeat
        # the whole phase would look frozen to the stale-run detector (it watches
        # ReportRun.updated_at), so a large, healthy course could be wrongly
        # reclaimed mid-report. Touching the run between calls keeps it alive.
        if run is not None:
            run.touch()
            db.commit()

    _ensure_document_summaries(db, course, markdowns, heartbeat)

    for doc in _document_reports(view, markdowns, heartbeat):
        report.sections.append(doc)
    metrics = build_metrics(view)
    _write_ai_readiness(course.title, view, metrics)
    heartbeat()
    report.metrics_json = metrics.to_json()
    # The fingerprint of the inputs this run saw, so a later re-run can tell
    # whether anything analysable has changed since (see
    # analysis.unchanged_since_last_report). Imported here, not at module
    # level: analysis imports this module to generate the report.
    from app.services.analysis import course_input_fingerprint

    report.snapshot_json = build_snapshot(view, course_input_fingerprint(course))
    report.priorities_md = _priorities_report(course.title, view)
    heartbeat()
    # Alignment and the never-applied checks are kept out of the Overall
    # summary: the summary synthesises the rule findings, while these two reach
    # the report on their own channels (a dedicated model pass, and the metrics).
    report.alignment_md = _write_alignment(db, course, view, markdowns)
    heartbeat()
    summary_md, solutions_md = _course_report(
        course.title,
        report.sections,
        metrics.passed,
        metrics.checked,
        view.never_applicable_rules(),
    )
    report.summary_md = summary_md
    report.solutions_md = solutions_md


def _document_markdowns(course: Course) -> dict[int, str]:
    """The full extracted Markdown of each document, keyed by document id."""
    out: dict[int, str] = {}
    for document in course.documents:
        markdown = document_markdown(document)
        if markdown:
            out[document.id] = markdown
    return out


def _facts_payload(verdict: RuleVerdict) -> list[dict]:
    """A rule's grounded facts shaped for the prompt template."""
    return [
        {"field": f.field, "value": f.value, "quotes": f.quotes}
        for f in verdict.facts
    ]


def _fit_markdown_to_context(render, markdown: str) -> str:
    """Clip a document's Markdown to the room left in the model's context.

    The rest of the prompt, the verdicts and their quotes, is kept intact.

    Args:
        render: Callable returning the full prompt for a given Markdown body.
        markdown: The document text to fit.

    Returns:
        The rendered prompt, within budget.
    """
    settings = get_settings()
    budget = char_budget(settings.generation_max_model_len, settings.report_output_token_budget)
    return clip_markdown_to_budget(render, markdown, budget)


def _document_report(
    filename: str, document_type: str, failures: list[RuleVerdict], markdown: str
) -> tuple[str, str]:
    if not failures:
        return "The document passed all automated checks.", ""
    failure_ctx = [
        {"text": v.rule.text, "rationale": v.rule.rationale, "facts": _facts_payload(v)}
        for v in failures
    ]
    client = _client()
    if not client.is_configured:
        return _fallback_document(filename, failures)
    prompt = _fit_markdown_to_context(
        lambda md: render_prompt(
            "report_document.j2",
            filename=filename,
            document_type=document_type,
            failures=failure_ctx,
            markdown=md,
        ),
        markdown,
    )
    schema = _doc_schema(len(failures))
    data = _ask(client, prompt, schema, normalize=False)
    # guided_json does not reliably hold the model to the pinned array length:
    # on a document with many findings it sometimes closes an array early,
    # producing schema-valid JSON with fewer entries than failures. That parses
    # cleanly, so nothing downstream can detect it, the missing entries would
    # silently degrade to the generic fallback line. A higher temperature
    # reliably produces the full array, so retry once before accepting one.
    if _under_filled(data.get("issues"), len(failures)) or _under_filled(
        data.get("solutions"), len(failures)
    ):
        logger.info(
            "Report model under-filled issues/solutions for %r (%d findings); retrying",
            filename,
            len(failures),
        )
        retry = _ask(client, prompt, schema, normalize=False, temperature=0.4)
        if not _under_filled(retry.get("issues"), len(failures)) and not _under_filled(
            retry.get("solutions"), len(failures)
        ):
            data = retry
    issues = _per_rule_bullets(data.get("issues", ""), failures, is_issue=True)
    solutions = _per_rule_bullets(data.get("solutions", ""), failures, is_issue=False)
    # Guard against the model dropping real findings: if rules failed but the
    # model returned an empty/trivial issues block, fall back to the
    # deterministic listing so a genuine failure is never reported as clean.
    if failures and len(issues.strip()) < 20:
        return _fallback_document(filename, failures)
    return issues, solutions


def _under_filled(value, expected: int) -> bool:
    """Whether an answer field carries fewer entries than were asked for.

    The schema always asks for an array of exactly that many items. A plain
    string is the one exception _per_rule_bullets also accepts, from older
    stored data or an unconstrained fallback model, and is complete on its own
    only when a single entry was expected.
    """
    if isinstance(value, str):
        return expected != 1
    return not isinstance(value, list) or len(value) != expected


def _document_reports(view, markdowns: dict[int, str], heartbeat) -> list[DocumentReportSection]:
    """Write one report section per document that has verdicts.

    Each document's prose is independent, so the model calls run concurrently in
    worker threads; _document_report touches no database, leaving every commit
    on this thread.

    Args:
        view: Report view holding the run's documents and verdicts.
        markdowns: The full Markdown of each document, by id.
        heartbeat: Called between documents to keep the run's progress fresh.

    Returns:
        The sections in the view's order, which is the display order regardless
        of the order the calls completed in.
    """
    docs = [doc for doc in view.documents if doc.verdicts]
    if not docs:
        return []

    def build_one(doc):
        failures = [v for v in doc.verdicts if v.outcome == "fail"]
        return _document_report(
            doc.filename, doc.document_type, failures, markdowns.get(doc.document_id, "")
        )

    concurrency = max(1, get_settings().report_generation_concurrency)
    results: dict[int, tuple[str, str]] = {}
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(build_one, doc): doc for doc in docs}
        for future in as_completed(futures):
            doc = futures[future]
            results[doc.document_id] = future.result()
            heartbeat()

    sections = []
    for doc in docs:
        issues_md, solutions_md = results[doc.document_id]
        sections.append(
            DocumentReportSection(
                document_id=doc.document_id,
                filename=doc.filename,
                document_type=doc.document_type,
                issues_md=issues_md,
                solutions_md=solutions_md,
            )
        )
    return sections


def _code_tag(rule) -> str:
    """The rule's identity tag, opening its report bullet.

    Prepended in code and never asked of the model, so every issue and
    suggestion deterministically names the exact rule behind it, the same code
    shown on the Rules page, in the analysis view and in the history compare.
    Empty for a legacy rule that has no code yet.
    """
    return f"**[{rule.code}]** " if rule.code else ""


def _per_rule_bullets(
    value, failures: list[RuleVerdict], is_issue: bool
) -> str:
    """Assemble the model's per-rule array into one Markdown bullet per failure.

    Entry i belongs to failure i, since the prompt lists them in that order and
    the schema pins the count, which also lets each bullet be prefixed with its
    rule's code deterministically. As a completeness net, an entry the model
    left blank or trivially short is replaced by the deterministic fallback line
    for that rule, so no failed rule can ever vanish from the prose.

    Args:
        value: The model's array, or a plain string from older stored data or an
            unconstrained fallback model, which passes through unchanged.
        failures: The failed verdicts, in prompt order.
        is_issue: Whether these are the issues rather than the solutions.

    Returns:
        The bullets as Markdown.
    """
    if isinstance(value, str):
        return clean_markdown(value)
    if not isinstance(value, (list, tuple)):
        return str(value)
    bullets = []
    for i, v in enumerate(failures):
        entry = str(value[i]).strip() if i < len(value) else ""
        entry = re.sub(r"^(?:[-*]\s+)+", "", entry)
        if len(entry) < 15:
            text = v.rule.text.rstrip(".")
            entry = (
                f"{text}.{_fallback_quotes(v)}"
                if is_issue
                else f"Address the failed check: {text}."
            )
        bullets.append(f"- {_code_tag(v.rule)}{entry}")
    return "\n".join(bullets)


def _write_ai_readiness(course_title, view, metrics: ReportMetrics) -> None:
    """Write the AI-readiness narrative onto the metrics, in place.

    The score itself is already deterministic, computed by build_metrics from
    the ai_readiness rules alone; this only adds the prose. The model is passed
    exactly those verdicts per document, which reasoning rule passed or failed
    and on which document, so the note explains the gauge instead of guessing a
    number. Left unset when there is no such rule or no model is configured.

    Args:
        course_title: Named in the prompt for context.
        view: Report view holding the verdicts.
        metrics: Metrics whose note is filled in.
    """
    if metrics.ai_readiness_score is None:
        return
    client = _client()
    if not client.is_configured:
        return
    docs = []
    for doc in view.documents:
        verdicts = [
            {"rule": v.rule.text, "outcome": v.outcome, "facts": _facts_payload(v)}
            for v in doc.verdicts
            if v.rule.category == RuleCategory.AI_READINESS
            and v.outcome in ("pass", "fail")
        ]
        if verdicts:
            docs.append({"filename": doc.filename, "verdicts": verdicts})
    prompt = render_prompt(
        "report_readiness.j2",
        course_title=course_title,
        score=metrics.ai_readiness_score,
        passed=metrics.ai_readiness_passed,
        checked=metrics.ai_readiness_checked,
        documents=docs,
    )
    data = _ask(client, prompt, _READINESS_SCHEMA)
    metrics.ai_readiness_note = str(data.get("note", "")).strip() or None


# Budgets for the constructive-alignment evidence: enough of each document to
# carry its real objectives / tasks, small enough that several assessments plus
# the syllabus never crowd the course prompt. The alignment check is a single
# extra judgement layered onto the existing course call, no new model call.
_ALIGNMENT_SYLLABUS_BUDGET = 4_000
_ALIGNMENT_ASSESSMENT_BUDGET = 1_500
_ALIGNMENT_MAX_ASSESSMENTS = 6


def _alignment_evidence(view, markdowns: dict[int, str]) -> dict | None:
    """Gather the grounding for the constructive-alignment judgement.

    Alignment asks whether the assessments exercise the objectives the syllabus
    states, which is cross-document, so the engine has no rule for it. The
    evidence handed to the model is the syllabus text, where the objectives are,
    plus a snippet of each assessment, which is what it actually asks; a gap may
    only be flagged from this. A pure read over existing Markdown.

    Args:
        view: Report view holding the documents.
        markdowns: The full Markdown of each document, by id.

    Returns:
        The evidence, or None when there is no syllabus or no assessment, in
        which case the prompt omits the section.
    """
    syllabus = next(
        (
            d
            for d in view.documents
            if DocumentType.SYLLABUS in DocumentType.parse(d.document_type)
            and markdowns.get(d.document_id, "").strip()
        ),
        None,
    )
    assessments = [
        d
        for d in view.documents
        if DocumentType.ASSESSMENT in DocumentType.parse(d.document_type)
        and markdowns.get(d.document_id, "").strip()
    ]
    if syllabus is None or not assessments:
        return None
    return {
        "syllabus_filename": syllabus.filename,
        "syllabus_text": markdowns[syllabus.document_id][:_ALIGNMENT_SYLLABUS_BUDGET],
        "assessments": [
            {
                "filename": d.filename,
                "text": markdowns[d.document_id][:_ALIGNMENT_ASSESSMENT_BUDGET],
            }
            for d in assessments[:_ALIGNMENT_MAX_ASSESSMENTS]
        ],
    }


_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"doc_summary": {"type": "string", "minLength": 1}},
    "required": ["doc_summary"],
    "additionalProperties": False,
}
_ALIGNMENT_SCHEMA = {
    "type": "object",
    "properties": {"alignment": {"type": "string"}},
    "required": ["alignment"],
    "additionalProperties": False,
}


def _document_summary(document, markdown: str) -> str:
    """Summarise one document.

    A pure model call with no database access, so it can run in a worker thread.

    Args:
        document: Document being summarised, for its name and type.
        markdown: Its full extracted text.

    Returns:
        The summary, or the empty string with no model configured or no answer.
    """
    client = _client()
    if not client.is_configured:
        return ""
    prompt = _fit_markdown_to_context(
        lambda md: render_prompt(
            "document_summary.j2",
            filename=document.filename,
            document_type=document.document_type,
            markdown=md,
        ),
        markdown,
    )
    data = _ask(client, prompt, _SUMMARY_SCHEMA)
    return str(data.get("doc_summary", "")).strip()


def _ensure_document_summaries(db, course: Course, markdowns, heartbeat) -> None:
    """Ensure every ready document version carries its cached summary.

    The summary names the document's important components: its topics, any
    stated learning objectives, graded tasks, worked examples and methods. That
    lets the course-level passes, constructive alignment first of all, reason
    across eighteen documents without re-reading nine hundred pages. It is
    stored on the immutable DocumentVersion, so an unchanged document costs its
    one summary call exactly once, ever, and a re-run reuses it for free.

    Summaries are independent across documents, so the calls run concurrently in
    worker threads, each touching nothing but its own document and Markdown;
    database writes stay on this thread.

    Args:
        db: Session the summaries are written through.
        course: Course whose documents to summarise.
        markdowns: The full Markdown of each document, by id.
        heartbeat: Called per document to keep the run's progress fresh.
    """
    client = _client()
    if not client.is_configured:
        return
    pending = []
    for document in course.documents:
        version = document.latest_version
        if version is None or version.status != DocumentStatus.READY:
            continue
        if version.summary_md:
            continue
        markdown = markdowns.get(document.id, "")
        if not markdown.strip():
            continue
        pending.append((document, version, markdown))
    if not pending:
        return

    concurrency = max(1, get_settings().report_generation_concurrency)
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(_document_summary, document, markdown): version
            for document, version, markdown in pending
        }
        for future in as_completed(futures):
            version = futures[future]
            summary = future.result()
            if summary:
                version.summary_md = summary
                db.commit()
            heartbeat()


def _write_alignment(db, course: Course, view, markdowns) -> str | None:
    """Write the constructive-alignment judgement, which is not rule-based.

    Each engine rule is evaluated on one document, so this cross-document
    question is asked of the model directly: do the objectives the syllabus
    states show up in the teaching material and get exercised by graded work?
    The evidence is the syllabus text plus each document's cached summary. One
    model call per report.

    Args:
        db: Session used to read the cached summaries.
        course: Course being judged.
        view: Report view holding the documents.
        markdowns: The full Markdown of each document, by id.

    Returns:
        The judgement, or None when there is nothing to align, on the same
        gating as _alignment_evidence.
    """
    evidence = _alignment_evidence(view, markdowns)
    if evidence is None:
        return None
    client = _client()
    if not client.is_configured:
        return None
    summaries = []
    for document in course.documents:
        version = document.latest_version
        if version is None or not version.summary_md:
            continue
        summaries.append(
            {
                "filename": document.filename,
                "document_type": document.document_type,
                "summary": version.summary_md,
            }
        )
    prompt = render_prompt(
        "report_alignment.j2",
        course_title=course.title,
        syllabus_filename=evidence["syllabus_filename"],
        syllabus_text=evidence["syllabus_text"],
        documents=summaries,
    )
    data = _ask(client, prompt, _ALIGNMENT_SCHEMA)
    return str(data.get("alignment", "")).strip() or None


def _course_report(
    course_title: str,
    sections: list[DocumentReportSection],
    passed: int,
    checked: int,
    never_applicable: list[dict] | None = None,
) -> tuple[str, str]:
    client = _client()
    if not client.is_configured:
        return _fallback_course(course_title, sections, passed, checked)
    prompt = render_prompt(
        "report_course.j2",
        course_title=course_title,
        sections=sections,
        section_count=len(sections),
        passed=passed,
        checked=checked,
        never_applicable=never_applicable or [],
    )
    data = _ask(client, prompt, _COURSE_SCHEMA)
    return data.get("summary", ""), data.get("solutions", "")


_EFFORT_LABEL = {QUICK_WIN: "quick win", "structural": "structural change"}


def _priorities_report(course_title: str, view) -> str | None:
    """Write the "fix these first" block over the top failures.

    The deterministic ranking (report_priorities) decides what is urgent and in
    what order; the model only phrases each item against the real document
    content.

    Args:
        course_title: Named in the prompt for context.
        view: Report view holding the verdicts.

    Returns:
        The block as Markdown, a plain deterministic list with no model
        configured, or None when nothing failed.
    """
    items = top_priorities(view)
    if not items:
        return None
    client = _client()
    if not client.is_configured:
        return _fallback_priorities(items)
    payload = [
        {
            "filename": p.filename,
            "rule_text": p.rule_text,
            "rationale": p.rationale,
            "severity": p.severity,
            "effort_label": _EFFORT_LABEL.get(p.effort, p.effort),
            "quotes": p.quotes,
        }
        for p in items
    ]
    prompt = render_prompt(
        "report_priorities.j2", course_title=course_title, priorities=payload
    )
    data = _ask(client, prompt, _PRIORITIES_SCHEMA)
    text = str(data.get("priorities", "")).strip()
    if not text:
        return _fallback_priorities(items)
    return _tag_priority_bullets(text, items)


def _tag_priority_bullets(text: str, items: list[PriorityItem]) -> str:
    """Prefix each priority bullet with its rule code.

    The prompt pins one bullet per item in the given order, so bullet i belongs
    to item i, and the code is prepended here rather than asked of the model, on
    the same policy as _per_rule_bullets.

    Args:
        text: The model's bullets.
        items: The priority items, in prompt order.

    Returns:
        The tagged text, or the text untouched when the model broke the
        one-bullet-per-item shape, since the pairing would then be a guess.
    """
    lines = text.splitlines()
    bullet_idx = [i for i, l in enumerate(lines) if re.match(r"^\s*[-*]\s+", l)]
    if len(bullet_idx) != len(items):
        return text
    for i, li in enumerate(bullet_idx):
        code = items[i].code
        if code:
            lines[li] = re.sub(r"^(\s*[-*]\s+)", rf"\1**[{code}]** ", lines[li])
    return "\n".join(lines)


def _fallback_priorities(items: list[PriorityItem]) -> str:
    """The priority list written deterministically, with no model configured."""
    lines = []
    for p in items:
        label = _EFFORT_LABEL.get(p.effort, p.effort)
        text = p.rule_text.rstrip(".")
        code = f"**[{p.code}]** " if p.code else ""
        lines.append(
            f"- {code}**{p.filename}**, {text} "
            f"({p.severity} severity, {label})."
        )
    return "\n".join(lines)


def _ask(
    client, prompt: str, schema: dict, normalize: bool = True, temperature: float = 0.0
) -> dict:
    """Make one constrained-JSON model call.

    Args:
        client: Configured report client.
        prompt: The rendered prompt.
        schema: Passed to the model as guided_json.
        normalize: Coerce each field to a Markdown string. The per-document
            report passes False, since its issues and solutions must reach
            _per_rule_bullets as real lists: normalising them to a joined string
            would bypass the one-entry-per-rule completeness net.
        temperature: Sampling temperature.

    Returns:
        The decoded answer, or an empty dict when it would not parse.
    """
    answer = client.generate(
        prompt,
        temperature=temperature,
        extra_body={
            **NO_THINKING,
            "guided_json": schema,
            # Without an explicit cap the endpoint's default applies, which can
            # be under what a document with several failed rules needs. The
            # answer is then cut off mid-string and loads_model_json cannot
            # repair a JSON that never closes. Matches the room the prompt
            # budgeting already reserved for the answer.
            "max_tokens": get_settings().report_output_token_budget,
        },
    )
    try:
        data = loads_model_json(answer)
    except ValueError:
        logger.warning("Report model returned non-JSON; using empty sections")
        return {}
    if not isinstance(data, dict):
        return {}
    if not normalize:
        return data
    return {k: _normalize_field(v) for k, v in data.items()}


def _normalize_field(value) -> str:
    """Coerce a report field to a Markdown string.

    Despite the schema pinning each field to a string, the model sometimes
    returns a list of bullet strings, or a pseudo-list inside a string. Both
    render as an ugly literal if stored as they are, so they are turned into a
    real bullet list. Non-string scalars are stringified and plain prose is left
    untouched.
    """
    if isinstance(value, (list, tuple)):
        # Items often arrive with their own leading bullet marker; strip it
        # before prefixing ours, or the page renders a doubled bullet.
        items = [
            re.sub(r"^(?:[-*]\s+)+", "", str(item).strip())
            for item in value
            if str(item).strip()
        ]
        return "\n".join(f"- {item}" for item in items if item)
    if not isinstance(value, str):
        return str(value)
    return clean_markdown(value)


def _fallback_quotes(verdict: RuleVerdict) -> str:
    """The verbatim source spans behind a verdict, as a readable clause.

    Reuses the same evidence the web view shows, with guard-only quotes
    filtered, rendered as prose: quoted text with its page, and never field
    names or raw values, so the fallback reads as natural language.
    """
    seen: list[str] = []
    for q in verdict.evidence_quotes:
        quote = (q.get("quote") or "").strip()
        if not quote or quote in seen:
            continue
        seen.append(quote)
        page = q.get("page")
        seen[-1] = f'“{quote}”' + (f" (p.{page})" if page else "")
    if not seen:
        return ""
    if len(seen) == 1:
        return f" For example: {seen[0]}."
    return " For example: " + "; ".join(seen) + "."


def _fallback_document(filename: str, failures: list[RuleVerdict]) -> tuple[str, str]:
    """The per-document report written deterministically, with no model.

    Plain natural language only, one readable sentence per failed check, with
    the offending passage quoted where there is one. No field names, values or
    other code-like fragments ever reach the page.

    Args:
        filename: Document being reported on.
        failures: Its failed verdicts.

    Returns:
        The issues text and an empty solutions text, which needs a model.
    """
    if not failures:
        return f"**{filename}** meets every check.", ""
    intro = (
        f"**{filename}** does not yet meet "
        f"{'this check' if len(failures) == 1 else f'{len(failures)} checks'}:"
    )
    lines = [intro, ""]
    for v in failures:
        text = v.rule.text.rstrip(".")
        lines.append(f"- {_code_tag(v.rule)}{text}.{_fallback_quotes(v)}")
    return "\n".join(lines), ""


def _fallback_course(
    course_title: str,
    sections: list[DocumentReportSection],
    passed: int,
    checked: int,
) -> tuple[str, str]:
    """The course-level report written deterministically, with no model.

    Args:
        course_title: Course being reported on.
        sections: The per-document sections already written.
        passed: How many checks passed.
        checked: How many ran.

    Returns:
        The summary text and an empty solutions text, which needs a model.
    """
    doc_word = "document" if len(sections) == 1 else "documents"
    summary = (
        f"## {course_title}\n\n"
        f"{passed} of {checked} checks passed across "
        f"{len(sections)} analysed {doc_word}."
    )
    if passed == checked:
        return summary, ""
    solutions = (
        "Work through the flagged checks document by document, starting with "
        "the highest-severity items in *Fix these first*. Each check above "
        "names exactly what is missing and quotes the passage that triggered it."
    )
    return summary, solutions
