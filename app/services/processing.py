"""Background processing pipeline for an uploaded document version.

Four steps: render every PDF page to a PNG and record its image hash; reuse
extractions for pages whose hash already has a successful extraction in this
course, preferring the same logical document; call the VLM only for genuinely
new or previously failed pages, at a configurable concurrency that defaults to
sequential; and embed the extracted pages for material RAG.

The version reaches READY only after embedding completes; a failure in any step
marks it FAILED. VLM calls run in worker threads but never touch the database:
all rows are written from the main thread to keep session handling trivial.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    Document,
    DocumentStatus,
    DocumentVersion,
    ExtractionSource,
    Page,
    PageExtraction,
    PageStatus,
)
from app.services import storage
from app.services.embedding import embed_version_pages
from app.services.extraction import extract_page_markdown
from app.services.pdf_render import render_pages

logger = logging.getLogger(__name__)


def process_document_version(version_id: int) -> None:
    """Run the whole pipeline for one uploaded version.

    The entry point of the documents worker. Owns its own session and never
    raises: any failure is recorded on the version as FAILED with its message.

    Args:
        version_id: Version to process; an unknown id is a no-op.
    """
    db = SessionLocal()
    version = db.get(DocumentVersion, version_id)
    if version is None:
        db.close()
        return
    try:
        version.status = DocumentStatus.RENDERING
        db.commit()
        pages = render_pages(db, version)
        db.commit()

        version.status = DocumentStatus.EXTRACTING
        db.commit()
        to_extract = _reuse_unchanged_pages(db, version, pages)
        db.commit()

        _extract_pages(db, to_extract)

        failed = [p for p in version.pages if p.status == PageStatus.FAILED]
        if failed:
            version.status = DocumentStatus.FAILED
            version.error_message = (
                f"Extraction failed for {len(failed)} page(s): "
                + ", ".join(str(p.page_number) for p in failed)
            )
            db.commit()
            return

        version.status = DocumentStatus.EMBEDDING
        db.commit()
        embed_version_pages(db, version)

        version.status = DocumentStatus.READY
        version.error_message = None
        db.commit()
    except Exception as exc:
        logger.exception("Processing failed for document version %s", version_id)
        db.rollback()
        version.status = DocumentStatus.FAILED
        version.error_message = f"Processing failed: {exc}"
        db.commit()
    finally:
        db.close()


def _reuse_unchanged_pages(
    db: Session, version: DocumentVersion, pages: list[Page]
) -> list[Page]:
    """Copy extractions for pages whose image already has one.

    Args:
        db: Session the copies are added to.
        version: Version being processed.
        pages: Its freshly rendered pages.

    Returns:
        The pages with nothing to reuse, which the VLM must still read.
    """
    to_extract: list[Page] = []
    for page in pages:
        source = _find_reusable_page(db, version, page)
        if source is None:
            to_extract.append(page)
            continue
        db.add(
            PageExtraction(
                page_id=page.id,
                markdown=source.extraction.markdown,
                source=ExtractionSource.REUSED,
                source_page_id=source.id,
                model_name=source.extraction.model_name,
            )
        )
        page.status = PageStatus.READY
    return to_extract


def _find_reusable_page(
    db: Session, version: DocumentVersion, page: Page
) -> Page | None:
    """Find a page in the course whose extraction this page can reuse.

    Matched on the rendered image hash, so the two pages are visually identical.
    Pages of the same logical document are preferred, then the most recent.

    Args:
        db: Session to query.
        version: Version being processed.
        page: Page looking for a source.

    Returns:
        The page to copy from, or None when the VLM must read this one.
    """
    course_id = version.document.course_id
    return db.execute(
        select(Page)
        .join(Page.extraction)
        .join(DocumentVersion, Page.document_version_id == DocumentVersion.id)
        .join(Document, DocumentVersion.document_id == Document.id)
        .where(
            Document.course_id == course_id,
            Page.image_hash == page.image_hash,
            Page.status == PageStatus.READY,
            Page.id != page.id,
        )
        .order_by(
            (Document.id == version.document_id).desc(),
            Page.id.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()


def _extract_pages(db: Session, pages: list[Page]) -> None:
    """Extract pages through the VLM, storing each result as it lands.

    Runs the calls in worker threads, but writes every row from this thread. A
    page whose call fails is marked FAILED with its message and the rest carry
    on, so one bad page does not lose the whole document's work.

    Args:
        db: Session the extractions are written through.
        pages: Pages the VLM must read.
    """
    if not pages:
        return
    concurrency = max(1, get_settings().page_extraction_concurrency)
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(
                extract_page_markdown,
                storage.abs_path(page.image_path),
                page.page_number,
            ): page
            for page in pages
        }
        for future in as_completed(futures):
            page = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                page.status = PageStatus.FAILED
                page.error_message = str(exc)
                db.commit()
                continue
            db.add(
                PageExtraction(
                    page_id=page.id,
                    markdown=result.markdown,
                    source=ExtractionSource.GENERATED,
                    model_name=result.model_name,
                )
            )
            page.status = PageStatus.READY
            page.error_message = None
            db.commit()
