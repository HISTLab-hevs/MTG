"""Course rule domain model.

A rule is a course-level requirement that documents of a given type must
satisfy. It has two formulations: the natural-language text shown to the user,
and the formal CEL condition the rule engine evaluates. Default rules ship with
both and are ready immediately; custom rules start from the professor's request
(source_prompt) and go through a background generation job: generating ->
proposed (awaiting review) -> ready when accepted, or failed when no valid CEL
could be produced.
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class RuleOrigin:
    """Whether a rule ships with the system or was written for one course."""

    DEFAULT = "default"
    CUSTOM = "custom"

    ALL = (DEFAULT, CUSTOM)


class RuleStatus:
    """Stages of CEL generation; only a ready rule is evaluated."""

    GENERATING = "generating"
    PROPOSED = "proposed"
    READY = "ready"
    FAILED = "failed"

    ALL = (GENERATING, PROPOSED, READY, FAILED)


class RuleSeverity:
    """How much a failure of the rule matters, for report ranking and charts."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    ALL = (LOW, MEDIUM, HIGH)
    WEIGHT = {LOW: 1, MEDIUM: 2, HIGH: 3}

    @classmethod
    def weight(cls, severity: str) -> int:
        """Numeric weight used to rank issues and score health.

        Args:
            severity: One of ALL.

        Returns:
            The weight, falling back to MEDIUM's so a bad or legacy value still
            ranks and scores reasonably.
        """
        return cls.WEIGHT.get(severity, cls.WEIGHT[cls.MEDIUM])


class RuleCategory:
    """Improvement theme a rule belongs to; one bar per category in the report.

    Attributes:
        LABELS: Display name per category, used in prose and tables. The stored
            value "assessment" collides with the document type Assessment, so it
            is shown as "Grading & feedback".
        SHORT_LABELS: Compact names, to keep radar-chart axes readable.
    """

    OUTCOMES = "outcomes"
    ASSESSMENT = "assessment"
    AI_READINESS = "ai_readiness"
    CONTENT = "content"
    CLARITY = "clarity"

    ALL = (OUTCOMES, ASSESSMENT, AI_READINESS, CONTENT, CLARITY)

    LABELS = {
        OUTCOMES: "Learning outcomes",
        ASSESSMENT: "Grading & feedback",
        AI_READINESS: "AI-readiness",
        CONTENT: "Content",
        CLARITY: "Clarity",
    }
    SHORT_LABELS = {
        OUTCOMES: "Outcomes",
        ASSESSMENT: "Grading",
        AI_READINESS: "AI-readiness",
        CONTENT: "Content",
        CLARITY: "Clarity",
    }

    @classmethod
    def label(cls, value: str) -> str:
        """Display name of a category, falling back to the raw value."""
        return cls.LABELS.get(value, (value or "").replace("_", " ").capitalize())

    @classmethod
    def short_label(cls, value: str) -> str:
        """Compact display name of a category, for chart axes."""
        return cls.SHORT_LABELS.get(value, cls.label(value))


class CourseRule(Base):
    """A requirement one course's documents must satisfy.

    Attributes:
        code: Short identifier, unique within the course (sy1, s3, a2, r1, sya1,
            all1): a type-derived prefix plus a per-prefix counter. Assigned
            once and never recomputed, it is the rule's identity across runs and
            is stored in report snapshots so runs can be compared by code.
            Nullable only for legacy rows, backfilled on the next course view.
        document_type: Non-empty subset of DocumentType.ALL, comma-separated; a
            rule applying to every document lists all four. A rule matches a
            document when their type sets overlap; read it with
            DocumentType.parse. Nullable only for legacy rows.
        text: The requirement in natural language, shown to the professor.
        rationale: One plain sentence on why the rule matters pedagogically,
            shown next to the rule and passed to the report so suggestions can
            explain the point of a fix.
        cel: The formal condition the engine evaluates.
        extraction_contract: JSON list of {name, type, agg, instruction}: the
            fields the CEL needs, declared by the rule itself. The report engine
            builds the extractor schema from the union of active contracts.
            Never shown to the user.
        severity: How much a failure matters; feeds ranking and charts.
        category: Improvement theme; feeds the course charts.
        source_prompt: The professor's original request, custom rules only.
        attempts: Generation attempts so far. During a retry, error_message
            holds the CEL validation error being corrected, so the UI can show
            progress.
        edit_draft: In-flight hand-edit being reconciled by the rules worker
            (JSON: status, the draft form values, and the completed contract
            when ready). Never read by evaluation; cleared on save or reopen.
    """

    __tablename__ = "course_rules"
    __table_args__ = (
        # Enforced here, not just assigned carefully, because next_rule_code's
        # "highest + 1" scan has no locking and two near-simultaneous requests
        # can compute the same code (see rules.add_custom_rule, which retries).
        Index(
            "ux_course_rules_course_id_code",
            "course_id",
            "code",
            unique=True,
            sqlite_where=text("code IS NOT NULL"),
            postgresql_where=text("code IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    # Default rules carry a fixed code from default_rules.yaml, identical in
    # every course; custom rules get the next free number at creation (see
    # app/services/rules.next_rule_code).
    code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    document_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    cel: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_contract: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Chosen by the model for custom rules, set by hand on the defaults.
    severity: Mapped[str] = mapped_column(String(10), default=RuleSeverity.MEDIUM)
    category: Mapped[str] = mapped_column(String(20), default=RuleCategory.CONTENT)
    origin: Mapped[str] = mapped_column(String(20), default=RuleOrigin.CUSTOM)
    status: Mapped[str] = mapped_column(String(20), default=RuleStatus.READY)
    source_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    edit_draft: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    course = relationship("Course", back_populates="rules")

    @property
    def is_generating(self) -> bool:
        """Whether the CEL is still being generated."""
        return self.status == RuleStatus.GENERATING
