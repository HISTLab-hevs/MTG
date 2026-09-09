"""Course analysis run: extract facts, evaluate rules, persist verdicts.

Triggered from the Report page. For every document in the course whose latest
version is ready, the engine collects the active rules targeting that document's
types, extracts the union of their contract fields page by page (one model call
per page, app.services.evidence), aggregates those facts into one doc object
(app.services.aggregation), runs each rule's CEL over it
(app.services.rule_engine), and writes one RuleEvaluation per version and rule.

Re-runs are incremental. A RuleEvaluation is keyed on the document version,
which is immutable, since a new upload makes a new version. A version whose
stored verdicts already cover exactly the current set of applicable rules is
therefore left untouched, with no re-extraction and no model calls. Only
versions that are new, replaced, or whose applicable rule set changed are
re-analysed; the document table, the charts and the whole course-level report
are always rebuilt from the full set of verdicts.
"""

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import SessionLocal
from app.llm.client import get_generation_client
from app.models import (
    Course,
    CourseRule,
    Document,
    DocumentStatus,
    DocumentType,
    DocumentVersion,
    EvaluationStatus,
    Page,
    ReportRun,
    RuleEvaluation,
    RuleStatus,
    RunStatus,
)
from app.services import contracts, rule_engine
from app.services.aggregation import build_document, build_evidence
from app.services.evidence import extract_page_facts

logger = logging.getLogger(__name__)


def run_analysis(report_run_id: int) -> None:
    """Evaluate every course rule over every ready document, then report.

    The entry point of the reports worker. Owns its own session, writes progress
    as it goes so the Report page can poll it, and marks the run failed with its
    message before re-raising, so RQ still records the failure.

    Args:
        report_run_id: Run to execute; an unknown id is a no-op.

    Raises:
        Exception: Whatever failed, after the run has been marked failed.
    """
    db = SessionLocal()
    run = db.get(ReportRun, report_run_id)
    if run is None:
        db.close()
        return
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
        rules = list(course.rules)
        for rule in rules:
            db.expunge(rule)
        ready = [
            d
            for d in course.documents
            if d.latest_version is not None
            and d.latest_version.status == DocumentStatus.READY
        ]

        to_analyse = []
        reused = 0
        for document in ready:
            version = document.latest_version
            if _is_up_to_date(db, version, document.types, rules):
                reused += 1
            else:
                to_analyse.append(document)
        run.status = RunStatus.RUNNING
        run.total_documents = len(ready)
        run.documents_done = reused
        db.commit()
        for document in to_analyse:
            version = document.latest_version
            run.current_document = document.filename
            run.current_pages_total = len(version.pages)
            run.current_pages_done = 0
            db.commit()
            _analyse_version(db, version, document.types, rules, run)
            run.documents_done += 1
            db.commit()
        run.current_document = None

        run.status = RunStatus.REPORTING
        db.commit()

        from app.services.report_generation import generate_report

        generate_report(run.id)
        run.status = RunStatus.COMPLETED
        db.commit()
    except Exception as exc:
        logger.exception("Analysis failed for report run %s", report_run_id)
        db.rollback()
        run.status = RunStatus.FAILED
        run.error_message = f"{type(exc).__name__}: {exc}"[:2000]
        db.commit()
        raise
    finally:
        db.close()


def _rule_definition_hash(rule: CourseRule) -> str:
    """Fingerprint what a rule's verdict actually depends on.

    Its CEL and the extraction contract that decides which facts feed that CEL.
    A hand-edit changes neither the rule's id nor its document-type scope, so
    this is what lets _is_up_to_date notice the edit.

    Args:
        rule: Rule to fingerprint.

    Returns:
        The hash, stored on every verdict the rule produces.
    """
    payload = json.dumps(
        {"cel": rule.cel or "", "contract": rule.extraction_contract or ""},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def course_input_fingerprint(course: Course) -> str:
    """Fingerprint everything an analysis run reads from the course.

    Two runs over the same fingerprint can only reach the same verdicts, so this
    is what tells a re-run apart from a repeat. It covers the exact document
    versions that would be analysed and the active rules with their definitions:
    uploading, replacing or deleting a document moves it, and so does adding,
    editing, disabling or deleting a rule.

    A rule's wording and rationale are deliberately not covered, being prose
    that cannot change a verdict, and neither is the report prose itself.

    Args:
        course: Course to fingerprint.

    Returns:
        The hash, frozen onto the run's snapshot and compared by
        unchanged_since_last_report.
    """
    documents = []
    for document in course.documents:
        version = document.latest_version
        if version is None or version.status != DocumentStatus.READY:
            continue
        documents.append(
            {
                "document": document.id,
                "version": version.id,
                "types": sorted(document.types),
            }
        )
    rules = [
        {
            "rule": r.id,
            "definition": _rule_definition_hash(r),
            "types": sorted(DocumentType.parse(r.document_type)),
        }
        for r in course.rules
        if r.is_active and r.status == RuleStatus.READY and r.cel
    ]
    payload = json.dumps(
        {
            "documents": sorted(documents, key=lambda d: d["document"]),
            "rules": sorted(rules, key=lambda r: r["rule"]),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def unchanged_since_last_report(db, course: Course) -> bool:
    """Whether re-running would analyse exactly what the last report already did.

    Running again would then cost the same model calls to reach the same
    verdicts and a freshly worded copy of the same report, so the UI refuses it
    and says what to change instead. Any real edit, a new or replaced document
    or a rule added, edited, switched off or deleted, moves the fingerprint and
    unblocks it.

    Args:
        db: Session used to load the last report.
        course: Course to check.

    Returns:
        True only when the answer is certain. Reports written before
        fingerprints were stored return False: refusing a run the professor is
        entitled to is the worse failure, so an unknown state always allows it.
    """
    from app.services.report_history import snapshot_fingerprint
    from app.services.report_view import latest_course_report

    report = latest_course_report(db, course)
    if report is None:
        return False
    previous = snapshot_fingerprint(report.snapshot_json)
    if previous is None:
        return False
    return previous == course_input_fingerprint(course)


def _is_up_to_date(
    db, version: DocumentVersion, document_types: list[str], rules: list[CourseRule]
) -> bool:
    """Whether a version's stored verdicts already match the current rules.

    A version is immutable, so its only reason to be re-analysed is a change in
    which rules apply to it, or a change to an applicable rule's own definition,
    which keeps the same id across a hand-edit.

    Args:
        db: Session to read the stored verdicts from.
        version: Version to check.
        document_types: Its document's types, deciding which rules apply.
        rules: The course's rules.

    Returns:
        True when every applicable rule has a stored verdict computed under its
        current definition. An added, edited or removed rule therefore forces a
        fresh pass, while an untouched document on a re-run costs no model
        calls.
    """
    applicable = {r.id: r for r in contracts.active_rules_for_types(rules, document_types)}
    if not applicable:
        return False
    evaluated = {
        rule_id: hash_
        for rule_id, hash_ in db.execute(
            select(RuleEvaluation.rule_id, RuleEvaluation.rule_definition_hash).where(
                RuleEvaluation.document_version_id == version.id
            )
        )
    }
    if set(evaluated) != set(applicable):
        return False
    return all(
        evaluated[rule_id] == _rule_definition_hash(rule)
        for rule_id, rule in applicable.items()
    )


def _analyse_version(
    db,
    version: DocumentVersion,
    document_types: list[str],
    rules: list[CourseRule],
    run: ReportRun,
) -> None:
    """Extract, evaluate and store every applicable rule's verdict for a version.

    The version's previous verdicts are dropped first, so the stored set always
    describes exactly this pass. A failed extraction records one failed verdict
    per rule rather than losing the document silently, and a rule whose CEL
    raises fails alone, leaving the others intact.

    Args:
        db: Session the verdicts are written through.
        version: Version to analyse.
        document_types: Its document's types, deciding which rules apply.
        rules: The course's rules.
        run: Run to report page progress on.
    """
    db.execute(
        delete(RuleEvaluation).where(
            RuleEvaluation.document_version_id == version.id
        )
    )
    db.commit()
    applicable = contracts.active_rules_for_types(rules, document_types)
    if not applicable:
        return

    fields = contracts.union_fields(applicable)
    try:
        page_facts = (
            _extract(version, fields, db, run)
            if fields
            else [{} for _ in version.pages]
        )
    except Exception as exc:
        for rule in applicable:
            db.add(
                RuleEvaluation(
                    document_version_id=version.id,
                    rule_id=rule.id,
                    status=EvaluationStatus.FAILED,
                    rule_definition_hash=_rule_definition_hash(rule),
                    error_message=f"Extraction failed: {exc}",
                )
            )
        db.commit()
        return

    model_name = get_generation_client().model
    for rule in applicable:
        rule_fields = contracts.contract_fields(rule)
        document = build_document(page_facts, rule_fields)
        evidence = build_evidence(page_facts, rule_fields)
        try:
            value = rule_engine.evaluate(rule, document)
            db.add(
                RuleEvaluation(
                    document_version_id=version.id,
                    rule_id=rule.id,
                    status=EvaluationStatus.EVALUATED,
                    value=value,
                    rule_definition_hash=_rule_definition_hash(rule),
                    activation=json.dumps({"doc": document, "evidence": evidence}),
                    model_name=model_name,
                )
            )
        except Exception as exc:
            db.add(
                RuleEvaluation(
                    document_version_id=version.id,
                    rule_id=rule.id,
                    status=EvaluationStatus.FAILED,
                    rule_definition_hash=_rule_definition_hash(rule),
                    error_message=f"Evaluation failed: {exc}",
                )
            )
    db.commit()


def _extract(
    version: DocumentVersion, fields: list[contracts.Field], db, run: ReportRun
) -> list[dict]:
    """Extract the contract fields from every page of a version.

    Pages are extracted concurrently: each model call runs in a worker thread
    and never touches the database, leaving every progress commit on this
    thread as calls complete. Progress is committed per page so the polling
    Report panel reflects it.

    Args:
        version: Version whose pages to read.
        fields: The union of the applicable rules' contract fields.
        db: Session used to commit progress.
        run: Run to record progress and defaulted pages on.

    Returns:
        One fact dict per page, re-sorted into page order, which
        build_document and build_evidence rely on.
    """
    concurrency = max(1, get_settings().evidence_extraction_concurrency)
    results: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(
                extract_page_facts,
                page.image_path,
                page.extraction.markdown if page.extraction else "",
                page.page_number,
                fields,
            ): page
            for page in version.pages
        }
        for future in as_completed(futures):
            page = futures[future]
            page_facts = future.result()
            if page_facts.pop("__defaulted__", False):
                run.pages_defaulted += 1
                logger.warning(
                    "Evidence extraction defaulted for document version %s page %s",
                    version.id,
                    page.page_number,
                )
            results[page.page_number] = page_facts
            run.current_pages_done += 1
            db.commit()
    return [results[n] for n in sorted(results)]
