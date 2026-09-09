"""Detect and reclaim analysis runs whose worker died mid-job.

A ReportRun is driven by a background worker (app.services.analysis). RQ caps a
job's wall time, but if the worker process itself dies, through an OOM, a
container restart or a hard crash, the job vanishes and nothing flips the run
out of its active state. The run stays active forever, so the Report page shows
"Analyzing..." indefinitely and the one-active-run guard blocks every re-run:
the course is wedged.

The run writes a heartbeat (ReportRun.updated_at) on every progress commit, so a
working run is never more than a few seconds stale. An active run whose
heartbeat is older than STALE_AFTER is treated as abandoned and flipped to
failed, which unblocks the re-run guard and lets the UI show an honest failure.
This runs opportunistically on the read paths that already load the run, so no
extra timer or worker is needed.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, update
from sqlalchemy import or_ as sa_or
from sqlalchemy.orm import Session

from app.models import ReportRun, RunStatus

# Active statuses a run can be wedged in if its worker disappears.
ACTIVE_STATUSES = RunStatus.ACTIVE

# How long a running or reporting run may go without a heartbeat before it
# counts as abandoned. Must stay well above the longest legitimate gap, or a
# slow-but-working run gets killed: the run commits after every extracted page
# and every report-phase model call, so the largest real gap is one model step,
# llm_request_timeout (120s) * (1 + llm_max_retries (2)) = 6 min worst case.
# This bound is about 3x that, derived from those two settings.
STALE_AFTER = timedelta(minutes=20)
# A PENDING run has no worker yet and never heartbeats, so it needs a far more
# lenient bound: a run merely waiting its turn must never be failed.
PENDING_STALE_AFTER = timedelta(hours=2)


def _is_stale(run: ReportRun) -> bool:
    """Whether an active run's heartbeat has gone cold for its status."""
    if run.status not in ACTIVE_STATUSES:
        return False
    updated = run.updated_at or run.created_at
    if updated.tzinfo is None:  # SQLite returns naive datetimes
        updated = updated.replace(tzinfo=timezone.utc)
    horizon = PENDING_STALE_AFTER if run.status == RunStatus.PENDING else STALE_AFTER
    return datetime.now(timezone.utc) - updated > horizon


def reclaim_if_stale(db: Session, run: ReportRun | None) -> ReportRun | None:
    """Mark one run failed if it is active and its heartbeat went cold.

    Idempotent and safe to call on every read of a run; commits only when it
    actually reclaims one.

    Args:
        db: Session to commit on.
        run: Run to check; None passes straight through.

    Returns:
        The same run, now failed, or the run unchanged.
    """
    if run is None or not _is_stale(run):
        return run
    run.status = RunStatus.FAILED
    run.current_document = None
    db.commit()
    return run


def reclaim_stale_runs(db: Session) -> int:
    """Sweep every abandoned active run to failed in one statement.

    Used by the course-wide queue accounting so a dead run never inflates the
    "N ahead of you" count or holds a phantom worker slot. A running or
    reporting run is abandoned after STALE_AFTER, since it heartbeats; a pending
    run only after the much longer PENDING_STALE_AFTER, since it does not.

    Args:
        db: Session to commit on.

    Returns:
        How many runs were reclaimed.
    """
    now = datetime.now(timezone.utc)
    result = db.execute(
        update(ReportRun)
        .where(
            ReportRun.status.in_(ACTIVE_STATUSES),
            sa_or(
                and_(
                    ReportRun.status.in_((RunStatus.RUNNING, RunStatus.REPORTING)),
                    ReportRun.updated_at < now - STALE_AFTER,
                ),
                and_(
                    ReportRun.status == RunStatus.PENDING,
                    ReportRun.updated_at < now - PENDING_STALE_AFTER,
                ),
            ),
        )
        .values(status=RunStatus.FAILED, current_document=None)
    )
    if result.rowcount:
        db.commit()
    return result.rowcount or 0
