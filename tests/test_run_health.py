"""Stale-run reclaim: a run whose worker died mid-job must not wedge the course.

A ReportRun heartbeats (updated_at) on every progress commit. If the worker
crashes, the run stays "running" forever — the Report page would show
"Analyzing…" indefinitely and the one-active-run guard would block every re-run.
run_health detects the cold heartbeat and flips the run to "failed", so the UI
unblocks and a re-run works. These tests pin that recovery.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import ReportRun
from app.services import run_health
from tests.conftest import login


def _run(db, course_id, status, minutes_cold):
    """Insert a run in `status` whose heartbeat is `minutes_cold` minutes old."""
    now = datetime.now(timezone.utc)
    stamp = now - timedelta(minutes=minutes_cold)
    run = ReportRun(
        course_id=course_id, status=status, created_at=stamp, updated_at=stamp
    )
    db.add(run)
    db.commit()
    return run


def test_running_run_with_cold_heartbeat_is_reclaimed(db, course):
    """A 'running' run untouched well past STALE_AFTER lost its worker → failed."""
    run = _run(db, course.id, "running", minutes_cold=30)
    reclaimed = run_health.reclaim_if_stale(db, run)
    assert reclaimed.status == "failed"
    assert reclaimed.current_document is None


def test_fresh_running_run_is_left_alone(db, course):
    """A run that heartbeat seconds ago is healthy and untouched."""
    run = _run(db, course.id, "running", minutes_cold=0)
    assert run_health.reclaim_if_stale(db, run).status == "running"


def test_slow_but_working_run_within_threshold_is_never_reclaimed(db, course):
    """SAFETY: a healthy run whose last heartbeat is a long single model step ago
    (one report call exhausting retries ≈ 6 min) is well under STALE_AFTER and
    must NOT be reclaimed — we must never kill a slow-but-working run."""
    minutes = run_health.STALE_AFTER.total_seconds() / 60
    # Just inside the window — the worst legitimate single-step gap and then some.
    run = _run(db, course.id, "running", minutes_cold=minutes - 2)
    assert run_health.reclaim_if_stale(db, run).status == "running"
    # The bulk sweep agrees.
    assert run_health.reclaim_stale_runs(db) == 0
    db.expire_all()
    assert db.get(ReportRun, run.id).status == "running"


def test_pending_run_waiting_in_queue_is_not_reclaimed(db, course):
    """A pending run does not heartbeat; merely waiting 30 min in a queue must
    NOT be failed (only the much longer pending bound applies)."""
    run = _run(db, course.id, "pending", minutes_cold=30)
    assert run_health.reclaim_if_stale(db, run).status == "pending"


def test_reclaim_stale_runs_sweeps_only_abandoned(db, course):
    """The bulk sweep fails a dead 'running' run but spares a fresh one and a
    normally-queued pending run."""
    from app.models import Course

    alive_course = Course(owner_id=course.owner_id, title="Alive")
    queued_course = Course(owner_id=course.owner_id, title="Queued")
    db.add_all([alive_course, queued_course])
    db.commit()
    dead = _run(db, course.id, "running", minutes_cold=30)
    alive = _run(db, alive_course.id, "reporting", minutes_cold=1)
    queued = _run(db, queued_course.id, "pending", minutes_cold=45)
    n = run_health.reclaim_stale_runs(db)
    db.expire_all()
    assert n == 1
    assert db.get(ReportRun, dead.id).status == "failed"
    assert db.get(ReportRun, alive.id).status == "reporting"
    assert db.get(ReportRun, queued.id).status == "pending"


def test_database_allows_only_one_course_report_per_run(db, course):
    from app.models import CourseReport, ReportStatus

    run = _run(db, course.id, "reporting", minutes_cold=0)
    db.add(CourseReport(report_run_id=run.id, status=ReportStatus.RUNNING))
    db.commit()

    db.add(CourseReport(report_run_id=run.id, status=ReportStatus.RUNNING))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_database_allows_only_one_active_run_per_course(db, course):
    _run(db, course.id, "pending", minutes_cold=0)

    duplicate = ReportRun(course_id=course.id, status="running")
    db.add(duplicate)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    # Historical runs do not occupy the active slot.
    active = db.execute(
        select(ReportRun).where(ReportRun.course_id == course.id)
    ).scalar_one()
    active.status = "completed"
    db.commit()
    _run(db, course.id, "pending", minutes_cold=0)


def test_report_build_heartbeats_so_reporting_phase_is_not_a_silent_gap(
    db, course, monkeypatch
):
    """The report-writing phase makes several model calls with no per-page commit;
    it must touch the run between them, or a large healthy course would look
    frozen to the stale detector. Assert _build calls run.touch()."""
    from app.models import CourseReport, ReportStatus
    from app.services import report_generation

    run = ReportRun(course_id=course.id, status="reporting")
    db.add(run)
    db.commit()
    report = CourseReport(report_run_id=run.id, status=ReportStatus.RUNNING)
    db.add(report)
    db.commit()

    # Each heartbeat() commits the run once. A silent reporting phase would
    # commit zero times; the course-level calls alone must account for two.
    touches = {"n": 0}
    real_touch = ReportRun.touch

    def counting_touch(self):
        touches["n"] += 1
        real_touch(self)

    monkeypatch.setattr(ReportRun, "touch", counting_touch)
    report_generation._build(db, course, report, run)
    assert touches["n"] >= 2


def test_report_page_reclaims_stale_run_and_unblocks_rerun(
    client, db, member, course, monkeypatch
):
    """End to end: a wedged 'running' run is reclaimed when the page is polled,
    and a subsequent re-run is allowed (enqueues a new run)."""
    _run(db, course.id, "running", minutes_cold=30)

    # Loading the report panel (what the 2s poll hits) reclaims the stale run.
    login(client, "user@example.com")
    resp = client.get(f"/courses/{course.id}/report/panel")
    assert resp.status_code == 200
    assert "didn't finish" in resp.text.lower()  # the failed-run hint
    db.expire_all()
    runs = db.execute(
        select(ReportRun).where(ReportRun.course_id == course.id)
    ).scalars().all()
    assert all(r.status == "failed" for r in runs)

    # A re-run is no longer blocked: it creates a new pending run.
    enqueued = []
    monkeypatch.setattr(
        "app.routers.courses.enqueue_analysis", lambda rid: enqueued.append(rid)
    )
    client.post(f"/courses/{course.id}/report/analyze", follow_redirects=False)
    db.expire_all()
    statuses = [
        r.status
        for r in db.execute(
            select(ReportRun).where(ReportRun.course_id == course.id)
        ).scalars()
    ]
    assert "pending" in statuses  # a fresh run was created
    assert len(enqueued) == 1
