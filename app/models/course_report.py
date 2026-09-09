"""The written course report produced after an analysis run.

One CourseReport per report run that reaches report generation. It holds two
course-level Markdown blocks plus one DocumentReportSection per analysed
document. The split that matters for the editing chat: summary_md and every
section's issues_md are the immutable digest of the deterministic rule verdicts
solutions_md blocks are the editable advice the professor can revise. See
app/services/report_generation.py.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ReportStatus:
    """Lifecycle of report generation, separate from the run's own status."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

    ALL = (PENDING, RUNNING, COMPLETED, FAILED)


class CourseReport(Base):
    """Course-level blocks of one run's report, with its document sections.

    Attributes:
        summary_md: Overall judgement of the course. Immutable.
        solutions_md: General advice, editable by the professor.
        solutions_md_prev: Value of solutions_md before the last edit, for Undo.
        priorities_md: "Fix these first", the most severe failures ranked.
            Immutable.
        alignment_md: Constructive-alignment judgement across documents. None
            when the course has no syllabus. Immutable.
        metrics_json: Chart data, persisted so charts render identically.
        snapshot_json: Frozen state of what this run saw, read by history.
    """

    __tablename__ = "course_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_run_id: Mapped[int] = mapped_column(
        ForeignKey("report_runs.id", ondelete="CASCADE"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default=ReportStatus.PENDING)
    summary_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    solutions_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    # One level of history is enough: it protects against the most common loss
    # (the model clobbering the block on a partial edit) without a version
    # table. See app/services/report_chat.py and routers/courses.py.
    solutions_md_prev: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Ranks the deterministic priorities and ties them to the real document
    # content, but invents no finding. See report_generation.
    priorities_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Written by its own model pass over the syllabus text and the per-document
    # summaries (document_versions.summary_md).
    alignment_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Computed deterministically from the run's verdicts, plus one
    # model-estimated readiness signal flagged as such. See
    # app/services/report_metrics.py.
    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Exact document versions, rule definitions, verdict facts and evidence.
    # Live evaluation rows may change on a later run, so history reads this
    # rather than reconstructing an earlier result. See report_history.py.
    snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    report_run = relationship("ReportRun", passive_deletes=True)
    sections = relationship(
        "DocumentReportSection",
        back_populates="report",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DocumentReportSection.id",
    )


class DocumentReportSection(Base):
    """One analysed document's part of the report.

    Attributes:
        issues_md: Grouped explanation of the deterministic failures. Immutable.
        solutions_md: Proposed improvements, editable via the report chat.
        solutions_md_prev: Value before the last edit, for one-step Undo.
    """

    __tablename__ = "document_report_sections"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_report_id: Mapped[int] = mapped_column(
        ForeignKey("course_reports.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(255))
    document_type: Mapped[str] = mapped_column(String(40))
    issues_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    solutions_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    solutions_md_prev: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    report = relationship("CourseReport", back_populates="sections")
