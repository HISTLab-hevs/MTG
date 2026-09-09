"""A course analysis run: status plus live progress for the Report page.

The progress columns are written incrementally by app.services.analysis as it
extracts each document page by page, so the polling Report panel can show an
overall bar and "document X of Y, page P of N". Because progress lives in the
database and not in the worker's memory, it survives a page refresh or the user
leaving and coming back while the job keeps running.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class RunStatus:
    """The only statuses a run may hold.

    ACTIVE ones occupy or await a report worker: the one-active-run guard and
    the stale-run reclaimer both key off this set, so a run stuck in a status
    outside it would show "Analyzing..." forever with no way to re-run. Create
    runs with PENDING.
    """

    PENDING = "pending"
    RUNNING = "running"
    REPORTING = "reporting"
    COMPLETED = "completed"
    FAILED = "failed"

    ACTIVE = (PENDING, RUNNING, REPORTING)
    ALL = (PENDING, RUNNING, REPORTING, COMPLETED, FAILED)


class ReportRun(Base):
    """One analysis of a course, from queued to completed or failed.

    The partial unique index allows only one active run per course.

    Attributes:
        status: One of RunStatus.ALL.
        pages_defaulted: Pages whose evidence extraction returned nothing
            usable and fell back to empty defaults, surfaced so silent gaps in
            the analysis are visible.
        error_message: Exception type and message of a failed run, so a run
            that dies long after its worker's logs are gone stays diagnosable.
        updated_at: Heartbeat, refreshed on every progress commit.
    """

    __tablename__ = "report_runs"
    __table_args__ = (
        Index(
            "ux_report_runs_one_active_per_course",
            "course_id",
            unique=True,
            sqlite_where=text(
                "status IN ('pending', 'running', 'reporting')"
            ),
            postgresql_where=text(
                "status IN ('pending', 'running', 'reporting')"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.PENDING)
    # Live extraction progress, updated as the run proceeds.
    total_documents: Mapped[int] = mapped_column(Integer, default=0)
    documents_done: Mapped[int] = mapped_column(Integer, default=0)
    current_document: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_pages_total: Mapped[int] = mapped_column(Integer, default=0)
    current_pages_done: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    pages_defaulted: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # A run still marked active whose updated_at is older than STALE_AFTER has
    # lost its worker (crash, OOM, restart) and is reclaimed as failed, so the
    # UI unblocks and a re-run becomes possible. See app.services.run_health.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    course = relationship("Course")

    def touch(self) -> None:
        """Refresh the heartbeat; call right before committing progress."""
        self.updated_at = datetime.now(timezone.utc)

    @property
    def progress_pct(self) -> int:
        """Overall percent: whole documents done plus the current one's pages.

        Returns:
            A value in 0..100; 0 while the document count is unknown.
        """
        if not self.total_documents:
            return 0
        done = self.documents_done
        if self.current_pages_total:
            done += self.current_pages_done / self.current_pages_total
        return min(100, round(100 * done / self.total_documents))
