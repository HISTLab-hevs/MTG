"""SQLAlchemy models, re-exported so callers import from app.models."""

from app.models.course import Course
from app.models.course_report import (
    CourseReport,
    DocumentReportSection,
    ReportStatus,
)
from app.models.document import (
    EMBEDDING_DIM,
    Document,
    DocumentStatus,
    DocumentType,
    DocumentVersion,
    ExtractionSource,
    Page,
    PageEmbedding,
    PageExtraction,
    PageStatus,
)
from app.models.evaluation import EvaluationStatus, RuleEvaluation
from app.models.report import ReportRun, RunStatus
from app.models.rule import (
    CourseRule,
    RuleCategory,
    RuleOrigin,
    RuleSeverity,
    RuleStatus,
)
from app.models.user import Role, User

__all__ = [
    "EMBEDDING_DIM",
    "Course",
    "CourseReport",
    "CourseRule",
    "Document",
    "DocumentReportSection",
    "DocumentStatus",
    "DocumentType",
    "DocumentVersion",
    "EvaluationStatus",
    "ExtractionSource",
    "Page",
    "PageEmbedding",
    "PageExtraction",
    "PageStatus",
    "ReportRun",
    "RunStatus",
    "ReportStatus",
    "RuleEvaluation",
    "Role",
    "RuleCategory",
    "RuleOrigin",
    "RuleSeverity",
    "RuleStatus",
    "User",
]
