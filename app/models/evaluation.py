"""Per-document rule verdicts produced by an analysis run.

One row per (document version, rule) the engine evaluated. See
app/services/analysis.py.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class EvaluationStatus:
    """Whether a row holds a real verdict or records why there is none."""

    EVALUATED = "evaluated"
    SKIPPED = "skipped"
    FAILED = "failed"

    ALL = (EVALUATED, SKIPPED, FAILED)


class RuleEvaluation(Base):
    """One rule's deterministic verdict on one document version.

    Attributes:
        value: The CEL verdict; None when skipped or failed.
        activation: JSON of the extracted facts the verdict ran over, kept for
            traceability; None when skipped or failed.
        rule_definition_hash: The rule's CEL and contract as of this verdict.
    """

    __tablename__ = "rule_evaluations"
    __table_args__ = (UniqueConstraint("document_version_id", "rule_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_version_id: Mapped[int] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("course_rules.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default=EvaluationStatus.EVALUATED)
    value: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # A hand-edit to a custom rule's CEL or contract does not change its id, so
    # analysis._is_up_to_date compares this hash alongside the rule id set: a
    # mismatch means the stored verdict was computed under a definition that no
    # longer exists, and forces a fresh pass. Always set by _analyse_version.
    rule_definition_hash: Mapped[str] = mapped_column(String(64))
    activation: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    rule = relationship("CourseRule", passive_deletes=True)
    document_version = relationship("DocumentVersion", passive_deletes=True)
