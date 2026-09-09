"""PDF upload handling.

The HTTP request only validates, hashes, stores the file and creates rows.
Rendering and VLM extraction always happen in a background job so uploads
stay fast regardless of document size.
"""

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Course, Document, DocumentStatus, DocumentType, DocumentVersion
from app.services import storage


class UploadError(Exception):
    """An upload the professor must fix: wrong type, bad file, too large."""


def handle_pdf_upload(
    db: Session,
    course: Course,
    filename: str | None,
    data: bytes,
    document_types: list[str],
) -> tuple[DocumentVersion | None, str]:
    """Validate and store an uploaded PDF as a new version.

    A document may be classified as several types at once, such as an exam that
    is also its own rubric; the rules of every chosen type then apply to it.

    Args:
        db: Session to commit on.
        course: Course receiving the upload.
        filename: Name as sent by the browser; it identifies the logical
            document, so re-uploading the same name adds a version to it.
        data: The file's bytes.
        document_types: The types the professor ticked; canonicalised here.

    Returns:
        The new version and a message for the professor. The version is None
        when the exact same PDF is already in the course and nothing needs
        processing.

    Raises:
        UploadError: No type chosen, not a PDF, or over the size limit.
    """
    settings = get_settings()
    filename = (filename or "").strip()

    chosen = [t for t in document_types if t in DocumentType.ALL]
    if not chosen:
        raise UploadError("Please select at least one document type.")
    stored_type = DocumentType.normalize(chosen)
    if not filename.lower().endswith(".pdf") or not data.startswith(b"%PDF-"):
        raise UploadError(
            "Only PDF files are accepted. Please convert your course material "
            "to PDF before uploading."
        )
    if len(data) > settings.max_upload_bytes:
        raise UploadError(
            f"The file exceeds the maximum upload size of {settings.max_upload_mb} MB."
        )

    sha256 = hashlib.sha256(data).hexdigest()

    # Serialize the short database part of uploads within a course: without
    # this lock, two requests for the same filename can both choose the same
    # next version number, and the same storage path, before either commits.
    # PostgreSQL locks the course row; SQLite ignores FOR UPDATE in tests.
    db.execute(
        select(Course.id).where(Course.id == course.id).with_for_update()
    ).scalar_one()

    # Same bytes already uploaded anywhere in this course: nothing to do.
    existing = db.execute(
        select(DocumentVersion)
        .join(Document)
        .where(Document.course_id == course.id, DocumentVersion.file_sha256 == sha256)
    ).scalar_one_or_none()
    if existing is not None:
        return None, (
            f"This exact PDF is already uploaded as “{existing.document.filename}” "
            f"(version {existing.version_number}). Nothing was re-processed."
        )

    # Same filename in the course means a new version of the logical document.
    document = db.execute(
        select(Document).where(
            Document.course_id == course.id, Document.filename == filename
        )
    ).scalar_one_or_none()
    if document is None:
        document = Document(
            course_id=course.id, filename=filename, document_type=stored_type
        )
        db.add(document)
        db.flush()
    else:
        # A new version of an existing document can re-classify it.
        document.document_type = stored_type

    # A per-document counter that only moves forward (see Document). Deriving
    # the number from the surviving versions would reuse it after the newest
    # upload is deleted, and reports record version numbers, so two different
    # PDFs would both read as "v3" of this document.
    version_number = document.next_version_number
    document.next_version_number = version_number + 1
    file_path = storage.save_original_pdf(course.id, document.id, version_number, data)

    version = DocumentVersion(
        document_id=document.id,
        version_number=version_number,
        file_sha256=sha256,
        file_path=file_path,
        file_size_bytes=len(data),
        status=DocumentStatus.UPLOADED,
    )
    db.add(version)
    db.commit()

    return version, f"“{filename}” uploaded (version {version_number}). Processing started."
