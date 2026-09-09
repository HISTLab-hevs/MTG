"""Document domain model.

The versioning chain is deliberately explicit:

    Course -> Document (logical file in a course, identified by filename)
           -> DocumentVersion (one specific uploaded PDF, identified by sha256)
           -> Page (one rendered page image of that version)
           -> PageExtraction (Markdown produced from that page image)

Keeping pages and extractions per version preserves full traceability of which
exact image the VLM saw, while Page.image_hash lets a new version reuse
extractions from identical pages of older versions instead of calling the VLM
again.
"""

from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

EMBEDDING_DIM = 768


class DocumentType:
    """The four kinds of course material, and how they are stored and shown.

    Attributes:
        LABELS: Display name per type. Documents and rules always carry at least
            one concrete type (a rule applying to every document lists all
            four), so there is no untyped "all documents" entry.
        DESCRIPTIONS: One-line gloss per type, shown beside the upload
            checkboxes so the professor classifies a document correctly; the
            type decides which rules apply.
    """

    SYLLABUS = "syllabus"
    SLIDE = "slide"
    ASSESSMENT = "assessment"
    RUBRIC = "rubric"

    ALL = (SYLLABUS, SLIDE, ASSESSMENT, RUBRIC)

    @staticmethod
    def parse(raw: str | None) -> list[str]:
        """Split a document's stored type field into its individual types.

        A document may carry several types at once, such as an exam paper that
        is also its own rubric. They are stored comma-separated in one column
        and the rules of every listed type apply.

        Args:
            raw: Stored type field, or None.

        Returns:
            The types in order, without duplicates or unknown entries, so the
            result is always a clean subset of ALL.
        """
        if not raw:
            return []
        seen: list[str] = []
        for part in raw.split(","):
            t = part.strip()
            if t in DocumentType.ALL and t not in seen:
                seen.append(t)
        return seen

    @staticmethod
    def normalize(types: list[str]) -> str:
        """Canonical stored form for a set of chosen types, in ALL order.

        Args:
            types: Chosen types, in any order.

        Returns:
            The comma-separated stored form.
        """
        return ",".join(t for t in DocumentType.ALL if t in types)

    LABELS = {
        SYLLABUS: "Syllabus",
        SLIDE: "Slides",
        ASSESSMENT: "Assessment",
        RUBRIC: "Rubric",
    }

    @staticmethod
    def label(raw: str | None) -> str:
        """Display label for a stored type field or a single rule target.

        Args:
            raw: Stored type field, possibly multi-type.

        Returns:
            The joined label ("assessment,rubric" -> "Assessment + Rubric"),
            falling back to the raw value for anything unrecognised.
        """
        types = DocumentType.parse(raw)
        if not types:
            return DocumentType.LABELS.get(raw, raw or "")
        return " + ".join(DocumentType.LABELS[t] for t in types)

    DESCRIPTIONS = {
        SYLLABUS: "The course outline: objectives, schedule, grading, policies.",
        SLIDE: "Teaching slides that explain the theory, concepts and examples.",
        ASSESSMENT: "An actual graded task: an exam, essay, project, or homework.",
        RUBRIC: "The criteria and levels used to grade student work.",
    }

    @staticmethod
    def describe(type_: str) -> str:
        """Short explanation of a single document type for the upload UI.

        Args:
            type_: One of ALL.

        Returns:
            The gloss, or the empty string for an unknown type.
        """
        return DocumentType.DESCRIPTIONS.get(type_, "")


class DocumentStatus:
    """Stages of a version's processing; PROCESSING groups the unfinished ones."""

    UPLOADED = "uploaded"
    RENDERING = "rendering"
    EXTRACTING = "extracting"
    EMBEDDING = "embedding"
    READY = "ready"
    FAILED = "failed"

    PROCESSING = (UPLOADED, RENDERING, EXTRACTING, EMBEDDING)
    ALL = (UPLOADED, RENDERING, EXTRACTING, EMBEDDING, READY, FAILED)


class PageStatus:
    """Whether a page has been extracted yet."""

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"

    ALL = (PENDING, READY, FAILED)


class ExtractionSource:
    """Whether an extraction came from a VLM call or was reused from a page."""

    GENERATED = "generated"
    REUSED = "reused"

    ALL = (GENERATED, REUSED)


class Document(Base):
    """A logical file in a course, holding its chain of uploaded versions.

    Attributes:
        document_type: Comma-separated subset of DocumentType.ALL, often a
            single type. Rules target documents through these; read it via
            types, not directly.
        next_version_number: The number the next upload will carry.
    """

    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("course_id", "filename"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    document_type: Mapped[str] = mapped_column(String(80))
    # A monotonic counter, not max(version_number) + 1: deleting the newest
    # upload must not hand its number to a different file, since reports record
    # version numbers and the professor reads "v3" as one specific PDF.
    next_version_number: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1", default=1
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    course = relationship("Course", back_populates="documents")
    versions = relationship(
        "DocumentVersion",
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="DocumentVersion.version_number",
    )

    @property
    def types(self) -> list[str]:
        """The document's types as a list; a rule applies if it targets any."""
        return DocumentType.parse(self.document_type)

    @property
    def latest_version(self) -> "DocumentVersion | None":
        """The most recent version, or None before the first upload."""
        return self.versions[-1] if self.versions else None


class DocumentVersion(Base):
    """One uploaded PDF of a document, with its rendered pages.

    Attributes:
        file_sha256: Digest of the uploaded bytes, identifying this version.
        summary_md: Compact model-written summary of the content (topics,
            stated objectives, graded work, methods, connections). Computed once
            at report time and reused by every later course-level pass: the
            version is immutable, so the summary never goes stale. See
            report_generation.
    """

    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    version_number: Mapped[int] = mapped_column(Integer)
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    file_path: Mapped[str] = mapped_column(String(500))
    file_size_bytes: Mapped[int] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=DocumentStatus.UPLOADED)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    document = relationship("Document", back_populates="versions")
    pages = relationship(
        "Page",
        back_populates="version",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Page.page_number",
    )

    @property
    def is_processing(self) -> bool:
        """Whether rendering, extraction or embedding is still under way."""
        return self.status in DocumentStatus.PROCESSING


class Page(Base):
    """One rendered page image of a version, as the VLM saw it.

    Attributes:
        image_hash: sha256 of the rendered PNG bytes; the key for extraction
            reuse across versions.
    """

    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("document_version_id", "page_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_version_id: Mapped[int] = mapped_column(
        ForeignKey("document_versions.id", ondelete="CASCADE"), index=True
    )
    page_number: Mapped[int] = mapped_column(Integer)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    image_path: Mapped[str] = mapped_column(String(500))
    image_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default=PageStatus.PENDING)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    version = relationship("DocumentVersion", back_populates="pages")
    extraction = relationship(
        "PageExtraction",
        back_populates="page",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
        foreign_keys="PageExtraction.page_id",
    )


class PageExtraction(Base):
    """The Markdown produced from a page image, with its provenance.

    Attributes:
        source: GENERATED for a real VLM call, REUSED for a copy.
        source_page_id: When reused, the page whose extraction was copied.
    """

    __tablename__ = "page_extractions"
    __table_args__ = (
        # Provenance must point somewhere else. source_page_id answers "which
        # VLM call did this row save?", so a row naming its own page answers
        # "I avoided reading this page by copying from this page", circular, and
        # it describes no event: an extraction the model actually produced has
        # source_page_id NULL and source "generated".
        #
        # Reachable, not hypothetical. A page trivially shares its own
        # image_hash, so on a re-process, where it is already READY with an
        # extraction, it satisfies every other filter in
        # processing._find_reusable_page and would be picked as its own source.
        # The single Page.id != page.id clause there is what prevents it; this
        # constraint keeps that true no matter how the query is rewritten.
        CheckConstraint(
            "source_page_id IS NULL OR source_page_id <> page_id",
            name="ck_page_extractions_source_not_self",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    page_id: Mapped[int] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"), unique=True
    )
    markdown: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default=ExtractionSource.GENERATED)
    # ON DELETE SET NULL, not CASCADE: the Markdown was copied at reuse time, so
    # losing the source page costs only the provenance link, never this row.
    #
    # Indexed for the sake of that clause. Postgres runs the SET NULL as one
    # "UPDATE page_extractions SET source_page_id = NULL WHERE source_page_id =
    # $1" per deleted page; unindexed this is a sequential scan of the largest
    # table in the schema, so deleting an N-page course costs N full scans.
    source_page_id: Mapped[int | None] = mapped_column(
        ForeignKey("pages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    model_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    page = relationship("Page", back_populates="extraction", foreign_keys=[page_id])


class PageEmbedding(Base):
    """One embedding vector per page, for material RAG retrieval.

    Both foreign keys cascade. page_id must: an embedding is meaningless
    without the page it encodes, and before the cascade existed, deleting a
    document whose pages had been embedded failed outright on this constraint.

    Attributes:
        course_id: Denormalized so a query can filter by course without joining
            the version/document chain. Derived from the page but never changing
            (a page cannot move between courses), so it cannot drift out of step.
        model_name: Which embedder produced the vector, so rows from an old
            model are recognizable and can be regenerated.
    """

    __tablename__ = "page_embeddings"

    id: Mapped[int] = mapped_column(primary_key=True)
    page_id: Mapped[int] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"), unique=True, index=True
    )
    course_id: Mapped[int] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    model_name: Mapped[str] = mapped_column(String(200))
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    page = relationship("Page")
