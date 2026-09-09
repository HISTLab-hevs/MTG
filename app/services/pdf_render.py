"""Page-aware PDF rendering.

Every page becomes a stored PNG plus a Page row carrying the sha256 of the
image bytes. Future analysis can therefore reference page anchors and visual
layout, and identical pages across versions are detected by hash.
"""

import hashlib

import pymupdf
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import DocumentVersion, Page, PageStatus
from app.services import storage


def render_pages(db: Session, version: DocumentVersion) -> list[Page]:
    """Render every page of a version to a PNG and record it.

    Sets the version's page_count as a side effect. The rows are flushed, not
    committed: the caller owns the transaction.

    Args:
        db: Session the new rows are added to.
        version: Version whose stored PDF to render.

    Returns:
        The new Page rows, in page order.
    """
    document = version.document
    pdf = pymupdf.open(storage.abs_path(version.file_path))
    try:
        version.page_count = pdf.page_count
        pages: list[Page] = []
        for index, pdf_page in enumerate(pdf):
            pixmap = pdf_page.get_pixmap(dpi=get_settings().pdf_render_dpi)
            png = pixmap.tobytes("png")
            image_path = storage.save_page_image(
                document.course_id,
                document.id,
                version.version_number,
                index + 1,
                png,
            )
            page = Page(
                document_version_id=version.id,
                page_number=index + 1,
                width=pixmap.width,
                height=pixmap.height,
                image_path=image_path,
                image_hash=hashlib.sha256(png).hexdigest(),
                status=PageStatus.PENDING,
            )
            db.add(page)
            pages.append(page)
        db.flush()
        return pages
    finally:
        pdf.close()
