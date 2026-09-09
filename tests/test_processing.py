"""End-to-end pipeline tests with the LLM call stubbed.

JOB_EAGER=true makes the upload request run the full pipeline inline, so the
document is fully processed by the time the upload response returns.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import (
    Document,
    DocumentStatus,
    DocumentType,
    DocumentVersion,
    ExtractionSource,
    Page,
    PageStatus,
)
from app.services import storage
from tests.conftest import login, make_pdf, upload_pdf


def _latest_version(db):
    document = db.execute(select(Document)).scalar_one()
    return document.latest_version


def test_uploaded_pdf_is_rendered_and_extracted(client, db, member, course):
    login(client, "user@example.com")
    upload_pdf(client, course.id, "slides.pdf", make_pdf(["Intro", "Processes"]))

    version = _latest_version(db)
    assert version.status == DocumentStatus.READY
    assert version.page_count == 2
    assert len(version.pages) == 2
    for page in version.pages:
        assert page.status == PageStatus.READY
        assert len(page.image_hash) == 64
        assert storage.abs_path(page.image_path).exists()
        assert page.extraction is not None
        assert page.extraction.source == ExtractionSource.GENERATED
        assert f"<!-- page: {page.page_number} -->" in page.extraction.markdown


def test_unchanged_pages_reuse_previous_extractions(client, db, member, course):
    login(client, "user@example.com")
    upload_pdf(client, course.id, "slides.pdf", make_pdf(["Intro", "Processes"]))
    v1 = _latest_version(db)
    v1_pages = {p.page_number: p for p in v1.pages}

    # Page 1 identical, page 2 changed.
    upload_pdf(client, course.id, "slides.pdf", make_pdf(["Intro", "Threads"]))

    db.expire_all()
    document = db.execute(select(Document)).scalar_one()
    v2 = document.versions[-1]
    assert v2.version_number == 2
    assert v2.status == DocumentStatus.READY

    page1, page2 = v2.pages
    assert page1.image_hash == v1_pages[1].image_hash
    assert page1.extraction.source == ExtractionSource.REUSED
    assert page1.extraction.source_page_id == v1_pages[1].id
    assert page1.extraction.markdown == v1_pages[1].extraction.markdown

    assert page2.image_hash != v1_pages[2].image_hash
    assert page2.extraction.source == ExtractionSource.GENERATED


def test_extraction_cannot_name_its_own_page_as_source(client, db, member, course):
    """Provenance must point at another page.

    `source_page_id` records which VLM call a row saved, so a row naming its
    own page describes no event: an extraction the model produced directly has
    source_page_id NULL. The reuse query excludes the page itself
    (processing._find_reusable_page), and the database now holds that invariant
    too, since a page trivially matches its own image_hash.
    """
    login(client, "user@example.com")
    upload_pdf(client, course.id, "slides.pdf", make_pdf(["Intro"]))

    db.expire_all()
    extraction = _latest_version(db).pages[0].extraction
    assert extraction.source_page_id is None  # generated, so no provenance

    extraction.source_page_id = extraction.page_id
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_third_version_processes_and_reuses_from_second(client, db, member, course):
    """A third upload of the same file works end to end: it is version 3, only the
    page that changed since v2 is re-extracted, and the pages unchanged since v2
    reuse v2's extraction (not v1's). This is the multi-version path the diff and
    the report iterate on."""
    login(client, "user@example.com")
    # v1: three pages.
    upload_pdf(client, course.id, "deck.pdf", make_pdf(["Intro", "Body", "End"]))
    # v2: page 2 changed; pages 1 and 3 identical to v1.
    upload_pdf(client, course.id, "deck.pdf", make_pdf(["Intro", "Body v2", "End"]))
    db.expire_all()
    document = db.execute(select(Document)).scalar_one()
    v2 = document.versions[-1]
    v2_pages = {p.page_number: p for p in v2.pages}
    # v3: page 3 changed; pages 1 and 2 identical to v2.
    upload_pdf(client, course.id, "deck.pdf", make_pdf(["Intro", "Body v2", "End v3"]))

    db.expire_all()
    document = db.execute(select(Document)).scalar_one()
    assert [v.version_number for v in document.versions] == [1, 2, 3]
    v3 = document.versions[-1]
    assert v3.status == DocumentStatus.READY
    assert v3.page_count == 3

    p1, p2, p3 = v3.pages
    # Page 1 unchanged since v2 -> reused, and reused FROM v2 (the latest match).
    assert p1.image_hash == v2_pages[1].image_hash
    assert p1.extraction.source == ExtractionSource.REUSED
    assert p1.extraction.source_page_id == v2_pages[1].id
    # Page 2 unchanged since v2 -> reused from v2.
    assert p2.image_hash == v2_pages[2].image_hash
    assert p2.extraction.source == ExtractionSource.REUSED
    assert p2.extraction.source_page_id == v2_pages[2].id
    # Page 3 changed in v3 -> freshly extracted.
    assert p3.image_hash != v2_pages[3].image_hash
    assert p3.extraction.source == ExtractionSource.GENERATED


def test_every_page_has_exactly_one_extraction(client, db, member, course):
    login(client, "user@example.com")
    upload_pdf(client, course.id, "a.pdf", make_pdf(["One", "Two"]))
    upload_pdf(client, course.id, "a.pdf", make_pdf(["One", "Two changed"]))
    upload_pdf(client, course.id, "b.pdf", make_pdf(["One"]))  # same page as a.pdf p1

    db.expire_all()
    documents = db.execute(select(Document)).scalars().all()
    for document in documents:
        for version in document.versions:
            for page in version.pages:
                assert page.extraction is not None
                assert page.extraction.source in (
                    ExtractionSource.GENERATED,
                    ExtractionSource.REUSED,
                )

    # b.pdf page 1 renders identically to a.pdf page 1 -> reused across documents.
    doc_b = next(d for d in documents if d.filename == "b.pdf")
    assert doc_b.latest_version.pages[0].extraction.source == ExtractionSource.REUSED


def test_page_extraction_progress_is_committed_incrementally(db, monkeypatch, course):
    import app.services.processing as processing
    from app.services.extraction import PageExtractionResult

    document = Document(
        course_id=course.id,
        filename="progress.pdf",
        document_type=DocumentType.SLIDE,
    )
    version = DocumentVersion(
        document=document,
        version_number=1,
        file_sha256="progress-sha",
        file_path="unused.pdf",
        file_size_bytes=123,
        page_count=3,
        status=DocumentStatus.EXTRACTING,
    )
    pages = [
        Page(
            version=version,
            page_number=page_number,
            width=100,
            height=100,
            image_path=f"unused/page-{page_number}.png",
            image_hash=f"hash-{page_number}",
            status=PageStatus.PENDING,
        )
        for page_number in range(1, 4)
    ]
    db.add(document)
    db.commit()

    def extract_page_markdown(image_path, page_number):
        return PageExtractionResult(
            page_number=page_number,
            markdown=f"page {page_number}",
            model_name="test-model",
        )

    ready_counts = []
    original_commit = db.commit

    def commit_and_observe():
        original_commit()
        with SessionLocal() as check_db:
            ready = check_db.execute(
                select(Page).where(
                    Page.document_version_id == version.id,
                    Page.status == PageStatus.READY,
                )
            ).scalars().all()
            ready_counts.append(len(ready))

    monkeypatch.setattr(processing, "extract_page_markdown", extract_page_markdown)
    monkeypatch.setattr(db, "commit", commit_and_observe)

    processing._extract_pages(db, pages)

    assert ready_counts == [1, 2, 3]
