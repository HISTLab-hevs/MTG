"""Course endpoints: the course list, the overview, and everything report-side.

The Analysis page starts a run and polls it; the report pages render one run's
result, live for the latest report and rebuilt from its frozen snapshot for an
older one. The two chats keep their history client-side and post it back each
turn, so the server holds no conversation state. Every route resolves the course
through get_owned_course, so a professor only ever reaches their own courses.
"""

import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_owned_course, require_member
from app.jobs.queue import enqueue_analysis
from app.models import (
    Course,
    CourseReport,
    DocumentReportSection,
    DocumentStatus,
    DocumentType,
    ReportRun,
    RunStatus,
    User,
)
from app.routers.documents import document_views
from app.services import run_health
from app.services.analysis import unchanged_since_last_report
from app.services.chat_compaction import compact_history
from app.services.courses import delete_course
from app.services.materials_chat import run_materials_chat
from app.services.report_chat import run_report_chat
from app.services.report_metrics import parse_metrics
from app.services.report_pdf import render_report_pdf
from app.services.report_priorities import checklist_by_document, top_priorities
from app.services.report_history import diff_reports
from app.services.report_snapshot_view import rebuild_view
from app.services.report_view import (
    build_report_view,
    completed_course_reports,
    latest_course_report,
)
from app.services.rules import ensure_default_rules
from app.templating import render_markdown, templates

router = APIRouter(prefix="/courses")

# Statuses that occupy a report worker (or are waiting to). Used both to count
# how busy the shared workers are and to place a still-queued run in line.
_ACTIVE_RUN_STATUSES = RunStatus.ACTIVE


@router.get("")
def course_list(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """List the professor's courses, newest first."""
    courses = list(
        db.execute(
            select(Course)
            .where(Course.owner_id == user.id)
            .order_by(Course.created_at.desc())
        ).scalars()
    )
    return templates.TemplateResponse(
        request, "courses/index.html", {"user": user, "courses": courses}
    )


@router.post("")
def create_course(
    title: str = Form(..., max_length=80),
    description: str = Form("", max_length=240),
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Create a course, seed it with the default rules, and open its overview."""
    course = Course(
        owner_id=user.id, title=title.strip(), description=description.strip() or None
    )
    db.add(course)
    db.commit()
    ensure_default_rules(db, course)
    return RedirectResponse(f"/courses/{course.id}/overview", status_code=303)


@router.get("/{course_id}")
def course_home(
    course_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Send a bare course URL to its overview."""
    course = get_owned_course(course_id, user, db)
    return RedirectResponse(f"/courses/{course.id}/overview", status_code=303)


@router.get("/{course_id}/edit")
def edit_course_form(
    course_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Open the edit dialog for a course: title and description."""
    course = get_owned_course(course_id, user, db)
    return templates.TemplateResponse(
        request, "courses/_course_edit_modal.html", {"course": course}
    )


@router.post("/{course_id}/edit")
def edit_course(
    course_id: int,
    title: str = Form(..., max_length=80),
    description: str = Form("", max_length=240),
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Save the professor's edit to a course's title and description.

    Raises:
        HTTPException: 422 when the title is left empty.
    """
    course = get_owned_course(course_id, user, db)
    title = title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="A course needs a title.")
    course.title = title
    course.description = description.strip() or None
    db.commit()
    return RedirectResponse("/courses", status_code=303)


@router.post("/{course_id}/delete")
def delete_course_route(
    course_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Delete a course with its documents, rules, runs and reports."""
    course = get_owned_course(course_id, user, db)
    delete_course(db, course)
    return RedirectResponse("/courses", status_code=303)


@router.get("/{course_id}/overview")
def overview(
    course_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Render the course overview: material counts, latest report, next step.

    The latest analysis and a run in flight are both surfaced here, so the page
    is useful at a glance instead of only linking away.
    """
    course = get_owned_course(course_id, user, db)
    views = document_views(course)
    stats = {
        "total": len(views),
        "processing": sum(1 for v in views if v["version"].is_processing),
        "ready": sum(1 for v in views if v["version"].status == DocumentStatus.READY),
        "failed": sum(1 for v in views if v["version"].status == DocumentStatus.FAILED),
    }
    report = latest_course_report(db, course)
    overview_report = None
    if report is not None:
        view = build_report_view(db, course)
        overview_report = {
            "report": report,
            "metrics": parse_metrics(report.metrics_json),
            "top": top_priorities(view, limit=3),
        }
    active_run = db.execute(
        select(ReportRun)
        .where(
            ReportRun.course_id == course.id,
            ReportRun.status.in_(_ACTIVE_RUN_STATUSES),
        )
        .order_by(ReportRun.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    active_run = run_health.reclaim_if_stale(db, active_run)
    if active_run is not None and active_run.status not in _ACTIVE_RUN_STATUSES:
        active_run = None
    # Until the first report exists the overview leads with the remaining step
    # instead of showing counters that are all zero, which is what a brand-new
    # course looks like. Only uploading and analysing are required: every course
    # already carries the default rule set, so tailoring the rules is an optional
    # detour and never the step being asked for.
    setup = {
        "has_materials": stats["total"] > 0,
        "has_ready_materials": stats["ready"] > 0,
        "active_rules": sum(1 for r in course.rules if r.is_active),
        "has_report": overview_report is not None,
    }
    if not setup["has_materials"]:
        setup["next_step"] = "materials"
    elif not setup["has_report"]:
        setup["next_step"] = "analysis"
    else:
        setup["next_step"] = None
    return templates.TemplateResponse(
        request,
        "courses/overview.html",
        {
            "user": user,
            "course": course,
            "stats": stats,
            "recent": views[:5],
            "overview_report": overview_report,
            "active_run": active_run,
            "setup": setup,
            "active_tab": "overview",
        },
    )


@router.get("/{course_id}/materials")
def materials(
    course_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Render the materials page: the document list and the upload form."""
    course = get_owned_course(course_id, user, db)
    return templates.TemplateResponse(
        request,
        "courses/materials.html",
        {
            "user": user,
            "course": course,
            "documents": document_views(course),
            "document_types": DocumentType.ALL,
            "alert": None,
            "active_tab": "materials",
        },
    )


@router.post("/{course_id}/materials/chat/compact")
def materials_chat_compact(
    course_id: int,
    history: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Compact the client-side materials chat memory before the next turn."""
    get_owned_course(course_id, user, db)
    result = compact_history(_parse_chat_history(history), "materials")
    return JSONResponse(result.as_payload())


@router.post("/{course_id}/materials/chat")
def materials_chat(
    course_id: int,
    request: Request,
    # Not Form(...): a required field answers a message-less POST with FastAPI's
    # raw validation JSON, and this endpoint's body is swapped straight into the
    # chat log, so the professor saw {"detail":[{"type":"missing"...}]} sitting
    # in the conversation. An empty send is a no-op, not an error.
    message: str = Form(""),
    history: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Read-only RAG turn: answer grounded in the course's materials.

    Args:
        message: The professor's question; an empty one is a no-op.
        history: Prior conversation as a JSON array, maintained client-side.
    """
    course = get_owned_course(course_id, user, db)
    if not message.strip():
        # 204: htmx is configured to swap nothing on this status, so the log is
        # left exactly as it was.
        return Response(status_code=204)
    result = run_materials_chat(
        db, course.id, message.strip(), _parse_chat_history(history)
    )
    return templates.TemplateResponse(
        request,
        "courses/_materials_chat_turn.html",
        {
            "message": message.strip(),
            "reply_html": result.reply_html,
            "sources": result.sources,
        },
    )


def _queue_position(db: Session, run: ReportRun | None) -> int:
    """How many other report runs are ahead of this one in the shared queue.

    Counts every still-active run across all courses created before this one, so
    a professor whose run waits behind busier courses sees an honest "N ahead of
    you".

    Args:
        db: Session used to sweep abandoned runs and count.
        run: Run to place in line; None or finished counts as 0.

    Returns:
        The number of runs ahead; 0 means this one is at the front, with a
        worker on it or next in line.
    """
    if run is None or run.status not in _ACTIVE_RUN_STATUSES:
        return 0
    # Sweep abandoned runs first so a dead run from another course never counts
    # as being "ahead of you" forever.
    run_health.reclaim_stale_runs(db)
    return db.execute(
        select(func.count())
        .select_from(ReportRun)
        .where(
            ReportRun.status.in_(_ACTIVE_RUN_STATUSES),
            ReportRun.created_at < run.created_at,
            ReportRun.id != run.id,
        )
    ).scalar_one()


def _report_context(db: Session, course: Course, user: User) -> dict:
    """Template context for the Analysis page and its polled panel.

    Returns:
        The context: the latest run with its queue position, the report view,
        every completed report of the course, and whether a re-run would repeat
        the last one exactly.
    """
    latest_run = db.execute(
        select(ReportRun)
        .where(ReportRun.course_id == course.id)
        .order_by(ReportRun.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    # If the latest run's worker died mid-job, its heartbeat is cold: reclaim it
    # to failed so the page shows an honest failure, with a working re-run,
    # instead of "Analyzing..." forever. Cheap, opportunistic, on every read.
    latest_run = run_health.reclaim_if_stale(db, latest_run)
    ready_count = sum(
        1
        for v in document_views(course)
        if v["version"].status == DocumentStatus.READY
    )
    # A run is genuinely "waiting" only while pending AND something is ahead of
    # it; once a worker picks it up the status moves to running/reporting.
    queue_ahead = (
        _queue_position(db, latest_run)
        if latest_run and latest_run.status == RunStatus.PENDING
        else 0
    )
    reports = completed_course_reports(db, course)
    return {
        "user": user,
        "course": course,
        "latest_run": latest_run,
        "ready_count": ready_count,
        "queue_ahead": queue_ahead,
        "report": build_report_view(db, course),
        "course_report": reports[0] if reports else None,
        "report_entries": [
            {"report": r, "metrics": parse_metrics(r.metrics_json)} for r in reports
        ],
        "unchanged": unchanged_since_last_report(db, course),
        "active_tab": "report",
    }


@router.get("/{course_id}/report")
def report(
    course_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Render the Analysis page: run controls, live progress, report list."""
    course = get_owned_course(course_id, user, db)
    return templates.TemplateResponse(
        request, "courses/report.html", _report_context(db, course, user)
    )


@router.get("/{course_id}/report/panel")
def report_panel(
    course_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Polled by htmx while an analysis runs."""
    course = get_owned_course(course_id, user, db)
    return templates.TemplateResponse(
        request, "courses/_report_panel.html", _report_context(db, course, user)
    )


@router.post("/{course_id}/report/analyze")
def run_report_analysis(
    course_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Queue an analysis of the course, unless one is already active.

    One active run per course is enough: a second would only duplicate work and
    take a worker slot from another user.
    """
    course = get_owned_course(course_id, user, db)
    # Nothing analysable has changed since the last report: re-running would
    # spend the same model calls to reach the same verdicts. The button is
    # already disabled with an explanation; this catches a stale tab or a
    # direct POST.
    if unchanged_since_last_report(db, course):
        return RedirectResponse(f"/courses/{course.id}/report", status_code=303)
    # First reclaim any abandoned run for this course (worker died mid-job): an
    # always-"running" run would otherwise block every future re-run. After this,
    # the guard sees only genuinely-active runs.
    run_health.reclaim_stale_runs(db)
    # The template disables the button too, but this is the authoritative check
    # against a double-submit or a stale tab.
    active = db.execute(
        select(ReportRun)
        .where(
            ReportRun.course_id == course.id,
            ReportRun.status.in_(_ACTIVE_RUN_STATUSES),
        )
        .limit(1)
    ).scalar_one_or_none()
    if active is None:
        run = ReportRun(course_id=course.id, status=RunStatus.PENDING)
        db.add(run)
        try:
            db.commit()
        except IntegrityError:
            # Another request won the one-active-run race after our SELECT.
            db.rollback()
            winner = db.execute(
                select(ReportRun.id)
                .where(
                    ReportRun.course_id == course.id,
                    ReportRun.status.in_(_ACTIVE_RUN_STATUSES),
                )
                .limit(1)
            ).scalar_one_or_none()
            if winner is None:
                raise
        else:
            enqueue_analysis(run.id)
    return RedirectResponse(f"/courses/{course.id}/report", status_code=303)


def _report_view_response(
    request: Request, db: Session, course: Course, report: CourseReport, user: User
):
    """Render one course report: the latest one live, an older one frozen.

    The latest report is the state of the course right now, so its checks are
    read from the live evaluation rows. An older report describes a run whose
    rows have since been overwritten, so its view is rebuilt from the snapshot
    frozen on it: the professor sees what that run actually found, not today's
    results under an old date.

    Args:
        request: Current request.
        db: Session used to resolve the live view.
        course: Course owning the report.
        report: Report to render.
        user: Signed-in professor.
    """
    latest = latest_course_report(db, course)
    is_latest = latest is not None and report.id == latest.id
    view = build_report_view(db, course) if is_latest else rebuild_view(report)
    # Failed verdicts per document id, so each section can show "why flagged",
    # the rule and its grounding quote, beneath its prose issues.
    findings_by_doc = {
        doc.document_id: [v for v in doc.verdicts if v.outcome == "fail"]
        for doc in view.documents
    }
    return templates.TemplateResponse(
        request,
        "courses/report_view.html",
        {
            "user": user,
            "course": course,
            "report": report,
            "metrics": parse_metrics(report.metrics_json),
            "summary_grid": view.summary_grid(),
            "findings_by_doc": findings_by_doc,
            "is_latest": is_latest,
            "active_tab": "report",
        },
    )


@router.get("/{course_id}/report/view")
def report_view(
    course_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Open the course's latest report, or the Analysis page when there is none."""
    course = get_owned_course(course_id, user, db)
    report = latest_course_report(db, course)
    if report is None:
        return RedirectResponse(f"/courses/{course.id}/report", status_code=303)
    return _report_view_response(request, db, course, report, user)


@router.get("/{course_id}/report/history")
def report_history(
    course_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Redirect kept so old links still land somewhere useful.

    The run history now lives on the Analysis page, which lists every report
    with compare and delete.
    """
    course = get_owned_course(course_id, user, db)
    return RedirectResponse(f"/courses/{course.id}/report", status_code=303)


@router.get("/{course_id}/report/compare")
def report_compare(
    course_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
    ids: Annotated[list[int], Query()] = [],
    reverse: bool = False,
):
    """Side-by-side of two of the course's reports: prose, verdicts, documents.

    The direction is chosen here, where both runs are on screen with their
    dates, because the list on the Analysis page only asks which two.

    Args:
        ids: The two reports to compare, as an unordered pair.
        reverse: Flip which one is the "before", recomputing the whole diff:
            what was resolved one way is newly broken the other.
    """
    course = get_owned_course(course_id, user, db)
    # Comparing a run with itself yields an empty diff that reads as "nothing
    # changed" when in truth nothing was compared; one run alone has nothing to
    # compare against. The picker prevents both; this catches a hand-typed or
    # bookmarked URL, and old links that still carry ?a=&b=.
    unique = list(dict.fromkeys(ids))
    if len(unique) != 2:
        return RedirectResponse(f"/courses/{course.id}/report", status_code=303)
    first, second = (_owned_report(db, course, i) for i in unique)
    # Chronological by default, so "before → after" reads as time passing
    # whichever order the two were ticked in.
    older, newer = sorted((first, second), key=lambda r: r.created_at)
    if reverse:
        older, newer = newer, older
    return templates.TemplateResponse(
        request,
        "courses/report_compare.html",
        {
            "user": user,
            "course": course,
            "older": older,
            "newer": newer,
            "diff": diff_reports(older, newer),
            "compare_ids": unique,
            "reversed_": reverse,
            "active_tab": "report",
        },
    )


# Declared after /report/history and /report/compare: a {report_id} path would
# otherwise swallow those literal segments and 422 on "history".
@router.get("/{course_id}/report/{report_id}/view")
def report_view_by_id(
    course_id: int,
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """One specific report of the course, opened from the report list."""
    course = get_owned_course(course_id, user, db)
    report = _owned_report(db, course, report_id)
    return _report_view_response(request, db, course, report, user)


@router.post("/{course_id}/report/{report_id}/chat/compact")
def report_chat_compact(
    course_id: int,
    report_id: int,
    history: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Compact the client-side report-refinement memory before the next turn."""
    course = get_owned_course(course_id, user, db)
    _owned_report(db, course, report_id)
    result = compact_history(_parse_chat_history(history), "report refinement")
    return JSONResponse(result.as_payload())


@router.post("/{course_id}/report/{report_id}/chat")
def report_chat(
    course_id: int,
    report_id: int,
    request: Request,
    # See materials_chat: an empty send must not answer with a validation blob
    # into the conversation log.
    message: str = Form(""),
    history: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Assistant turn: reply, and live-update any editable block it changed.

    Args:
        message: The professor's message; an empty one is a no-op.
        history: Prior conversation as a JSON array of {role, content},
            maintained client-side so the assistant has memory without server
            state.
    """
    course = get_owned_course(course_id, user, db)
    report = _owned_report(db, course, report_id)
    if not message.strip():
        return Response(status_code=204)
    result = run_report_chat(
        db, report, message.strip(), _parse_chat_history(history)
    )
    changed_blocks = [
        b
        for b in _editable_block_descriptors(course, report)
        if b["target"] in result.changed
    ]
    return templates.TemplateResponse(
        request,
        "courses/_chat_turn.html",
        {
            "message": message.strip(),
            # Rendered the same way the report and the materials chat do, so
            # bold and lists show as formatting, not raw asterisks. The history
            # sync reads textContent, so memory is unaffected by the wrapping.
            "reply_html": render_markdown(result.reply),
            "changed_blocks": changed_blocks,
            # A document only recognised from prose scopes the turn but is not
            # read, so it must not claim "read the full text" here.
            "context_filename": (
                None if result.context_from_prose else result.context_filename
            ),
            "context_truncated": result.context_truncated,
            "unresolved_mentions": result.unresolved_mentions or [],
            "ignored_mentions": result.ignored_mentions or [],
        },
    )


def _parse_chat_history(raw: str) -> list[dict]:
    """Decode the client-maintained chat history, keeping only valid turns.

    The browser may include one hidden summary turn produced by the compactor,
    plus user and assistant turns with optional millisecond timestamps. Old
    turns are not dropped here: compaction handles long memory, and the prompt
    builder is the final budget safety net.

    Args:
        raw: The history as posted by the browser, a JSON array.

    Returns:
        The valid turns, in order; an empty list for anything unparsable.
    """
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    turns = []
    for t in data:
        if (
            not isinstance(t, dict)
            or t.get("role") not in ("user", "assistant", "summary")
            or not isinstance(t.get("content"), str)
        ):
            continue
        item = {"role": t["role"], "content": str(t["content"])}
        if isinstance(t.get("at"), (int, float)) and t["at"] > 0:
            item["at"] = int(t["at"])
            item["time"] = datetime.fromtimestamp(
                item["at"] / 1000, timezone.utc
            ).isoformat(timespec="seconds")
        if isinstance(t.get("time"), str):
            item["time"] = t["time"]
        turns.append(item)
    return turns


def _editable_block_descriptors(course: Course, report: CourseReport) -> list[dict]:
    """Render descriptors for the editable blocks, keyed by chat target id.

    Args:
        course: Course owning the report.
        report: Report whose blocks to describe.

    Returns:
        One descriptor per editable block: the course-level suggestions first,
        then one per document section.
    """
    base = f"/courses/{course.id}/report"
    blocks = [
        {
            "target": "course",
            "dom_id": "editable-course",
            "label": "Course-level suggestions",
            "value": report.solutions_md,
            "action": f"{base}/{report.id}/solutions",
            "undo_action": f"{base}/{report.id}/solutions/undo",
            "undoable": report.solutions_md_prev is not None,
        }
    ]
    for s in report.sections:
        blocks.append(
            {
                "target": f"section:{s.id}",
                "dom_id": f"editable-section-{s.id}",
                "label": f"Suggestions for {s.filename}",
                "value": s.solutions_md,
                "action": f"{base}/section/{s.id}/solutions",
                "undo_action": f"{base}/section/{s.id}/solutions/undo",
                "undoable": s.solutions_md_prev is not None,
            }
        )
    return blocks


def _owned_report(db: Session, course: Course, report_id: int) -> CourseReport:
    """Resolve one of the course's reports by id.

    Returns:
        The report.

    Raises:
        HTTPException: 404 when it belongs to another course.
    """
    report = db.get(CourseReport, report_id)
    if report is None or report.report_run is None or (
        report.report_run.course_id != course.id
    ):
        raise HTTPException(status_code=404)
    return report


def _editable_block_response(
    request: Request,
    course: Course,
    report: CourseReport,
    target: str,
    undone: bool = False,
):
    """Swap just the edited block back after a manual save.

    Keeping the swap to one block means the page does not reload and the
    professor keeps their scroll position; a non-htmx submit falls back to a
    full redirect.

    Args:
        request: Current request.
        course: Course owning the report.
        report: Report the block belongs to.
        target: Chat target id of the block to swap back.
        undone: Whether the block was just stepped backwards, so the control
            must read "Redo" rather than "Undo". The state is a single swap of
            current and previous text, so the direction cannot be read off the
            rows; it is carried here from the route that performed the step.
    """
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(f"/courses/{course.id}/report/view", status_code=303)
    block = next(
        b
        for b in _editable_block_descriptors(course, report)
        if b["target"] == target
    )
    return templates.TemplateResponse(
        request,
        "courses/_editable_block.html",
        {"dom_id": block["dom_id"], "value": block["value"],
         "action": block["action"], "undo_action": block["undo_action"],
         "undoable": block["undoable"], "undone": undone, "oob": False},
    )


def _save_solutions_edit(db: Session, entity, solutions_md: str) -> None:
    """Write an edit to an editable block, keeping the previous text for undo.

    Shared by the course-level and per-section edit routes: only the entity
    differs, the "was this a real change" guard and the previous-text
    bookkeeping are identical for both.

    Args:
        db: Session to commit on.
        entity: The CourseReport or DocumentReportSection being edited.
        solutions_md: The professor's new text.
    """
    if (entity.solutions_md or "") != (solutions_md or ""):
        entity.solutions_md_prev = entity.solutions_md
        entity.solutions_md = solutions_md
        db.commit()


def _undo_solutions_edit(db: Session, entity) -> None:
    """Restore an editable block to the text it held before the last edit.

    Swaps current and previous, so a second click redoes it. This is the safety
    net for the chat clobbering a multi-part block.

    Args:
        db: Session to commit on.
        entity: The CourseReport or DocumentReportSection to step back.
    """
    if entity.solutions_md_prev is not None:
        entity.solutions_md, entity.solutions_md_prev = (
            entity.solutions_md_prev, entity.solutions_md)
        db.commit()


def _owned_section(db: Session, course: Course, section_id: int) -> DocumentReportSection:
    """Resolve one of the course's report sections by id.

    Returns:
        The section.

    Raises:
        HTTPException: 404 when it belongs to another course.
    """
    section = db.get(DocumentReportSection, section_id)
    if section is None:
        raise HTTPException(status_code=404)
    _owned_report(db, course, section.course_report_id)  # 404s on mismatch
    return section


@router.post("/{course_id}/report/{report_id}/solutions")
def edit_course_solutions(
    course_id: int,
    report_id: int,
    request: Request,
    solutions_md: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Save the professor's edit to the course-level suggestions."""
    course = get_owned_course(course_id, user, db)
    report = _owned_report(db, course, report_id)
    _save_solutions_edit(db, report, solutions_md)
    return _editable_block_response(request, course, report, "course")


@router.post("/{course_id}/report/section/{section_id}/solutions")
def edit_section_solutions(
    course_id: int,
    section_id: int,
    request: Request,
    solutions_md: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Save the professor's edit to one document's suggestions."""
    course = get_owned_course(course_id, user, db)
    section = _owned_section(db, course, section_id)
    report = _owned_report(db, course, section.course_report_id)
    _save_solutions_edit(db, section, solutions_md)
    return _editable_block_response(request, course, report, f"section:{section_id}")


@router.post("/{course_id}/report/{report_id}/solutions/undo")
def undo_course_solutions(
    course_id: int,
    report_id: int,
    request: Request,
    undone: bool = Form(False),
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Step the course block back one edit, or forward again if already back.

    Args:
        undone: What the pressed button offered: false for Undo, true for Redo.
            Posted by the button itself, since the swap of current and previous
            leaves no trace of direction in the row. It alternates with each
            press, so the control always names what the next press will do.
    """
    course = get_owned_course(course_id, user, db)
    report = _owned_report(db, course, report_id)
    _undo_solutions_edit(db, report)
    return _editable_block_response(
        request, course, report, "course", undone=not undone
    )


@router.post("/{course_id}/report/section/{section_id}/solutions/undo")
def undo_section_solutions(
    course_id: int,
    section_id: int,
    request: Request,
    undone: bool = Form(False),
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Step one document's suggestions back one edit, or forward again.

    Args:
        undone: What the pressed button offered; see undo_course_solutions.
    """
    course = get_owned_course(course_id, user, db)
    section = _owned_section(db, course, section_id)
    report = _owned_report(db, course, section.course_report_id)
    _undo_solutions_edit(db, section)
    return _editable_block_response(
        request, course, report, f"section:{section_id}", undone=not undone
    )


def _report_pdf_response(db: Session, course: Course, report: CourseReport):
    """Render one report as a PDF download, checklist included.

    Same latest-or-archived split as the on-screen view: the checklist of an
    older report comes from its own frozen snapshot, so the file matches the run
    it is named after.

    Args:
        db: Session used to resolve the live view.
        course: Course owning the report.
        report: Report to render.
    """
    latest = latest_course_report(db, course)
    view = (
        build_report_view(db, course)
        if latest is not None and report.id == latest.id
        else rebuild_view(report)
    )
    pdf = render_report_pdf(course, report, checklist=checklist_by_document(view))
    stamp = report.created_at.strftime("%Y-%m-%d")
    filename = f"{course.title or 'course'}-report-{stamp}.pdf".replace(" ", "_")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{course_id}/report/view/pdf")
def report_pdf(
    course_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Download the course's latest report as PDF."""
    course = get_owned_course(course_id, user, db)
    report = latest_course_report(db, course)
    if report is None:
        return RedirectResponse(f"/courses/{course.id}/report", status_code=303)
    return _report_pdf_response(db, course, report)


@router.get("/{course_id}/report/{report_id}/view/pdf")
def report_pdf_by_id(
    course_id: int,
    report_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Download one specific report of the course as PDF."""
    course = get_owned_course(course_id, user, db)
    report = _owned_report(db, course, report_id)
    return _report_pdf_response(db, course, report)


@router.post("/{course_id}/report/{report_id}/delete")
def delete_report(
    course_id: int,
    report_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Remove one analysis from the course's history.

    Deletes the run as well: the report is the only readable product of a
    completed run, so leaving the run behind would keep a row nobody can open.
    The cascade takes the report, its sections and the run's own rows with it.
    The live evaluation rows are deliberately untouched: they belong to the
    documents' current state, not to this run.
    """
    course = get_owned_course(course_id, user, db)
    report = _owned_report(db, course, report_id)
    run = report.report_run
    db.delete(report)
    if run is not None:
        db.delete(run)
    db.commit()
    return RedirectResponse(f"/courses/{course.id}/report", status_code=303)
