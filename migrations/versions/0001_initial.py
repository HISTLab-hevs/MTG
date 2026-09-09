"""initial tables

Revision ID: 0001
Revises:
Create Date: 2026-06-11

"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from app.models.document import EMBEDDING_DIM

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=False),
        sa.Column("last_name", sa.String(length=100), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "courses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "owner_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_courses_owner_id", "courses", ["owner_id"])

    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "course_id",
            sa.Integer(),
            sa.ForeignKey("courses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(length=255), nullable=False),
        # Comma-separated subset of the document types (a document may be several).
        sa.Column("document_type", sa.String(length=80), nullable=False),
        # The number the NEXT upload of this document will carry. Held here
        # rather than derived from max(version_number) because a deleted newest
        # version would hand its number back to a different file, and reports
        # store version numbers: two distinct PDFs would both read as "v3".
        # Only ever moves forward, so a gap is visible as what it is.
        sa.Column(
            "next_version_number", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("course_id", "filename"),
    )
    op.create_index("ix_documents_course_id", "documents", ["course_id"])

    op.create_table(
        "document_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Integer(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        # Cached model summary of the version's content, reused by course-level
        # report passes (constructive alignment).
        sa.Column("summary_md", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("document_id", "version_number"),
    )
    op.create_index(
        "ix_document_versions_document_id", "document_versions", ["document_id"]
    )
    op.create_index(
        "ix_document_versions_file_sha256", "document_versions", ["file_sha256"]
    )

    op.create_table(
        "pages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "document_version_id",
            sa.Integer(),
            sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("image_path", sa.String(length=500), nullable=False),
        sa.Column("image_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.UniqueConstraint("document_version_id", "page_number"),
    )
    op.create_index("ix_pages_document_version_id", "pages", ["document_version_id"])
    op.create_index("ix_pages_image_hash", "pages", ["image_hash"])

    op.create_table(
        "page_extractions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "page_id",
            sa.Integer(),
            sa.ForeignKey("pages.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        # SET NULL: the reused Markdown was copied into this row, so deleting
        # the page it came from costs only the provenance link.
        sa.Column(
            "source_page_id",
            sa.Integer(),
            sa.ForeignKey("pages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("model_name", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # Provenance must point at another page: a row copied "from itself"
        # describes no event (see app.models.document.PageExtraction).
        sa.CheckConstraint(
            "source_page_id IS NULL OR source_page_id <> page_id",
            name="ck_page_extractions_source_not_self",
        ),
    )
    # Supports the SET NULL above: without it, every deleted page triggers a
    # sequential scan of this table.
    op.create_index(
        "ix_page_extractions_source_page_id", "page_extractions", ["source_page_id"]
    )

    op.create_table(
        "page_embeddings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "page_id",
            sa.Integer(),
            sa.ForeignKey("pages.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "course_id",
            sa.Integer(),
            sa.ForeignKey("courses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_page_embeddings_course_id", "page_embeddings", ["course_id"])

    op.create_table(
        "report_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "course_id",
            sa.Integer(),
            sa.ForeignKey("courses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("total_documents", sa.Integer(), nullable=False),
        sa.Column("documents_done", sa.Integer(), nullable=False),
        sa.Column("current_document", sa.String(length=255), nullable=True),
        sa.Column("current_pages_total", sa.Integer(), nullable=False),
        sa.Column("current_pages_done", sa.Integer(), nullable=False),
        # Why a failed run failed — kept on the row so it outlives worker logs.
        sa.Column("error_message", sa.Text(), nullable=True),
        # Pages whose evidence extraction fell back to empty defaults.
        sa.Column(
            "pages_defaulted", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # Heartbeat: bumped on every progress commit so a run whose worker died
        # mid-job can be detected as stale and reclaimed (flipped to failed).
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_report_runs_course_id", "report_runs", ["course_id"])
    op.create_index(
        "ux_report_runs_one_active_per_course",
        "report_runs",
        ["course_id"],
        unique=True,
        sqlite_where=sa.text(
            "status IN ('pending', 'running', 'reporting')"
        ),
        postgresql_where=sa.text(
            "status IN ('pending', 'running', 'reporting')"
        ),
    )

    op.create_table(
        "course_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "course_id",
            sa.Integer(),
            sa.ForeignKey("courses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code", sa.String(length=16), nullable=True),
        sa.Column("document_type", sa.String(length=80), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("cel", sa.Text(), nullable=True),
        sa.Column("extraction_contract", sa.Text(), nullable=True),
        sa.Column("severity", sa.String(length=10), nullable=False),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("source_prompt", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("edit_draft", sa.Text(), nullable=True),
        sa.Column("model_name", sa.String(length=200), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_course_rules_course_id", "course_rules", ["course_id"])
    op.create_index(
        "ux_course_rules_course_id_code",
        "course_rules",
        ["course_id", "code"],
        unique=True,
        sqlite_where=sa.text("code IS NOT NULL"),
        postgresql_where=sa.text("code IS NOT NULL"),
    )

    op.create_table(
        "rule_evaluations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "document_version_id",
            sa.Integer(),
            sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "rule_id",
            sa.Integer(),
            sa.ForeignKey("course_rules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("value", sa.Boolean(), nullable=True),
        sa.Column("rule_definition_hash", sa.String(length=64), nullable=False),
        sa.Column("activation", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("model_name", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("document_version_id", "rule_id"),
    )
    op.create_index(
        "ix_rule_evaluations_document_version_id",
        "rule_evaluations",
        ["document_version_id"],
    )
    op.create_index(
        "ix_rule_evaluations_rule_id", "rule_evaluations", ["rule_id"]
    )

    op.create_table(
        "course_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "report_run_id",
            sa.Integer(),
            sa.ForeignKey("report_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("summary_md", sa.Text(), nullable=True),
        sa.Column("solutions_md", sa.Text(), nullable=True),
        sa.Column("solutions_md_prev", sa.Text(), nullable=True),
        sa.Column("priorities_md", sa.Text(), nullable=True),
        # Constructive alignment: the cross-document pass's own block.
        sa.Column("alignment_md", sa.Text(), nullable=True),
        sa.Column("metrics_json", sa.Text(), nullable=True),
        sa.Column("snapshot_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "report_run_id", name="uq_course_reports_report_run_id"
        ),
    )
    op.create_table(
        "document_report_sections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "course_report_id",
            sa.Integer(),
            sa.ForeignKey("course_reports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("document_type", sa.String(length=40), nullable=False),
        sa.Column("issues_md", sa.Text(), nullable=True),
        sa.Column("solutions_md", sa.Text(), nullable=True),
        sa.Column("solutions_md_prev", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_document_report_sections_course_report_id",
        "document_report_sections",
        ["course_report_id"],
    )


def downgrade() -> None:
    op.drop_table("document_report_sections")
    op.drop_table("course_reports")
    op.drop_table("rule_evaluations")
    op.drop_table("course_rules")
    op.drop_table("report_runs")
    op.drop_table("page_embeddings")
    op.drop_table("page_extractions")
    op.drop_table("pages")
    op.drop_table("document_versions")
    op.drop_table("documents")
    op.drop_table("courses")
    op.drop_table("users")
