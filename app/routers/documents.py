"""Course material endpoints: upload, browse, download and delete documents.

Uploads are stored and enqueued for processing here; the page-level extraction
itself runs on the documents worker. Every route resolves the course through
get_owned_course, so a professor only ever reaches their own material.
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import get_owned_course, require_member
from app.jobs.queue import enqueue_document_processing
from app.models import Course, Document, DocumentVersion, Page, PageStatus, User
from app.services import storage
from app.services.documents import delete_document, delete_version
from app.services.uploads import UploadError, handle_pdf_upload
from app.templating import templates

router = APIRouter(prefix="/courses/{course_id}")


def document_views(course: Course) -> list[dict]:
    """Flatten each document and its latest version for the templates.

    Args:
        course: Course whose documents to list.

    Returns:
        One entry per document that has at least one version, with its page
        counts for the progress display.
    """
    views = []
    for document in course.documents:
        version = document.latest_version
        if version is None:
            continue
        views.append(
            {
                "document": document,
                "version": version,
                "pages_total": version.page_count or 0,
                "pages_done": sum(
                    1 for p in version.pages if p.status == PageStatus.READY
                ),
            }
        )
    return views


def _document_area_context(request: Request, course: Course, alert=None) -> dict:
    """Template context for the materials panel.

    Args:
        request: Current request.
        course: Course being shown.
        alert: Optional (level, message) pair to display above the list.

    Returns:
        The context, including whether any version is still processing.
    """
    documents = document_views(course)
    return {
        "request": request,
        "course": course,
        "documents": documents,
        "any_processing": any(v["version"].is_processing for v in documents),
        "alert": alert,
    }


async def _read_capped(file: UploadFile, limit: int) -> bytes | None:
    """Read the upload in chunks, stopping as soon as it exceeds a limit.

    A plain await file.read() materialises the whole body before any size check,
    so an oversized upload could exhaust the process's memory; this caps it at
    limit plus one chunk.

    Args:
        file: Upload being read.
        limit: Maximum accepted size, in bytes.

    Returns:
        The bytes, or None when the upload is too large.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/materials/upload")
async def upload_material(
    course_id: int,
    request: Request,
    file: UploadFile = File(...),
    document_type: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Accept a PDF, store it as a new version, and queue its processing.

    Answers with the materials panel either way: a rejected upload comes back as
    an error alert, since the panel is swapped in place by htmx.

    Args:
        document_type: The types the professor ticked; they decide which rules
            will apply to the document.
    """
    course = get_owned_course(course_id, user, db)
    settings = get_settings()
    data = await _read_capped(file, settings.max_upload_bytes)
    if data is None:
        return templates.TemplateResponse(
            request,
            "courses/_document_area.html",
            _document_area_context(
                request,
                course,
                (
                    "error",
                    "The file exceeds the maximum upload size of "
                    f"{settings.max_upload_mb} MB.",
                ),
            ),
        )
    try:
        version, message = handle_pdf_upload(
            db, course, file.filename, data, document_type
        )
        alert = ("success" if version else "info", message)
        if version:
            enqueue_document_processing(version.id)
        db.expire_all()  # eager-mode jobs update rows behind this session
    except UploadError as exc:
        alert = ("error", str(exc))
    return templates.TemplateResponse(
        request,
        "courses/_document_area.html",
        _document_area_context(request, course, alert),
    )


@router.get("/materials/list")
def materials_list(
    course_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Re-render the materials panel; polled while a version is processing."""
    course = get_owned_course(course_id, user, db)
    return templates.TemplateResponse(
        request,
        "courses/_document_area.html",
        _document_area_context(request, course),
    )


def _version_view(version) -> dict:
    """View model of one version.

    The extracted Markdown is an internal artefact the professor never needs to
    see, so the UI shows the original document only; the page counts are kept
    for the processing progress and status.

    Args:
        version: Version being displayed.

    Returns:
        Extracted page count and the numbers of the failed pages.
    """
    return {
        "extracted_pages": sum(1 for p in version.pages if p.extraction),
        "failed_pages": [
            p.page_number for p in version.pages if p.status == PageStatus.FAILED
        ],
    }


def _previous_version(document: Document, version):
    """The version uploaded just before another one, or None if it is the first.

    Args:
        document: Document owning both versions.
        version: Version to look back from.

    Returns:
        The preceding version, or None.
    """
    prev = None
    for v in document.versions:  # ordered by version_number (ascending)
        if v.id == version.id:
            return prev
        prev = v
    return None


def _version_diff(document: Document, version) -> dict | None:
    """Page-level diff against the previous version, from image hashes.

    Two pages with the same rendered-image hash are visually identical, so the
    professor can be told exactly which pages changed, were added, or removed
    when they re-uploaded: no model call, no guesswork.

    Args:
        document: Document owning the version.
        version: Version to compare.

    Returns:
        The changed, added and removed page numbers with the unchanged count, or
        None when this is the first version.
    """
    prev = _previous_version(document, version)
    if prev is None:
        return None
    old = {p.page_number: p.image_hash for p in prev.pages}
    new = {p.page_number: p.image_hash for p in version.pages}
    changed = sorted(n for n in new if n in old and new[n] != old[n])
    added = sorted(n for n in new if n not in old)
    removed = sorted(n for n in old if n not in new)
    unchanged = sum(1 for n in new if n in old and new[n] == old[n])
    return {
        "prev_number": prev.version_number,
        "changed": changed,
        "added": added,
        "removed": removed,
        "unchanged": unchanged,
        "has_changes": bool(changed or added or removed),
    }


def _get_owned_document(
    course_id: int, document_id: int, user: User, db: Session
) -> tuple[Course, Document]:
    """Resolve a document, checking it belongs to a course the user owns.

    Returns:
        The course and the document.

    Raises:
        HTTPException: 404 when either is not the user's.
    """
    course = get_owned_course(course_id, user, db)
    document = db.get(Document, document_id)
    if document is None or document.course_id != course.id:
        raise HTTPException(status_code=404, detail="Document not found")
    return course, document


def _selected_version(document: Document, version_id: int | None):
    """The requested version of a document, or the latest one.

    Args:
        document: Document to read from.
        version_id: Version asked for; None selects the latest.

    Returns:
        The matching version.

    Raises:
        HTTPException: 404 when the id names no version of this document.
    """
    if version_id is None:
        return document.latest_version
    for version in document.versions:
        if version.id == version_id:
            return version
    raise HTTPException(status_code=404, detail="Version not found")


@router.get("/documents/{document_id}")
def document_detail(
    course_id: int,
    document_id: int,
    request: Request,
    version_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Render one document's page, with its version history and diff."""
    course, document = _get_owned_document(course_id, document_id, user, db)
    version = _selected_version(document, version_id)
    return templates.TemplateResponse(
        request,
        "documents/detail.html",
        {
            "user": user,
            "course": course,
            "document": document,
            "version": version,
            "view": _version_view(version),
            "diff": _version_diff(document, version),
            "active_tab": "materials",
        },
    )


@router.get("/documents/{document_id}/panel")
def document_panel(
    course_id: int,
    document_id: int,
    request: Request,
    version_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Render the version panel alone, swapped in when another version is picked."""
    course, document = _get_owned_document(course_id, document_id, user, db)
    version = _selected_version(document, version_id)
    return templates.TemplateResponse(
        request,
        "documents/_version_panel.html",
        {
            "course": course,
            "document": document,
            "version": version,
            "view": _version_view(version),
            "diff": _version_diff(document, version),
        },
    )


@router.get("/documents/{document_id}/file")
def document_file(
    course_id: int,
    document_id: int,
    version_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Serve the original PDF inline so the browser's viewer can display it."""
    course, document = _get_owned_document(course_id, document_id, user, db)
    version = _selected_version(document, version_id)
    return FileResponse(
        storage.abs_path(version.file_path),
        media_type="application/pdf",
        filename=document.filename,
        content_disposition_type="inline",
    )


@router.post("/documents/{document_id}/delete")
def delete_document_route(
    course_id: int,
    document_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Delete a document with every version, then return to the materials list."""
    course, document = _get_owned_document(course_id, document_id, user, db)
    delete_document(db, document)
    return RedirectResponse(f"/courses/{course.id}/materials", status_code=303)


@router.post("/documents/{document_id}/versions/{version_id}/delete")
def delete_version_route(
    course_id: int,
    document_id: int,
    version_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Remove one upload of a document, from the version list under a material.

    The last remaining version takes the whole document with it: a document with
    no versions has nothing to show, analyse or open, and the confirmation the
    professor answered says so.

    Raises:
        HTTPException: 404 when the version is not this document's, 409 while it
            is still being processed.
    """
    course, document = _get_owned_document(course_id, document_id, user, db)
    version = next((v for v in document.versions if v.id == version_id), None)
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    # A version being rendered or extracted has a worker writing its pages and
    # files right now; deleting the rows underneath it would leave that job
    # writing into a deleted subtree.
    if version.is_processing:
        raise HTTPException(
            status_code=409,
            detail="This version is still being processed. Wait for it to finish.",
        )
    delete_version(db, document, version)
    return RedirectResponse(f"/courses/{course.id}/materials", status_code=303)


@router.get("/documents/{document_id}/pages/{page_id}/image")
def page_image(
    course_id: int,
    document_id: int,
    page_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_member),
):
    """Serve one rendered page image, as the VLM saw it."""
    _, document = _get_owned_document(course_id, document_id, user, db)
    # The join scopes the page to this document, enforcing ownership in the query
    # rather than by scanning every version's pages in Python: this is requested
    # once per thumbnail.
    image_path = db.execute(
        select(Page.image_path)
        .join(DocumentVersion)
        .where(Page.id == page_id, DocumentVersion.document_id == document.id)
    ).scalar_one_or_none()
    if image_path is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return FileResponse(storage.abs_path(image_path), media_type="image/png")
