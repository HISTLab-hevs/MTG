"""Deleting a course, a document, or a single version leaves nothing behind.

Rows, stored files, and embeddings all go; an extraction another document
reuses survives its source. SQLite enforces foreign keys here (see app.db),
so these exercise the same cascades Postgres applies in production.
"""

from sqlalchemy import func, select

from app.models import (
    Course,
    Document,
    DocumentVersion,
    ExtractionSource,
    Page,
    PageEmbedding,
    PageExtraction,
)
from app.services import storage
from tests.conftest import login, make_client, make_pdf, upload_pdf


def _document(db, filename):
    return db.execute(
        select(Document).where(Document.filename == filename)
    ).scalar_one_or_none()


def _count(db, model):
    return db.execute(select(func.count()).select_from(model)).scalar_one()


def test_delete_document_removes_rows_and_files(client, db, member, course):
    login(client, "user@example.com")
    upload_pdf(client, course.id, "slides.pdf", make_pdf(["Intro", "Processes"]))
    document = _document(db, "slides.pdf")
    files_dir = storage.abs_path(f"courses/{course.id}/documents/{document.id}")
    assert files_dir.exists()

    response = client.post(
        f"/courses/{course.id}/documents/{document.id}/delete",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/courses/{course.id}/materials"

    db.expire_all()
    assert _document(db, "slides.pdf") is None
    assert not files_dir.exists()


def test_delete_preserves_reused_extractions_of_other_documents(
    client, db, member, course
):
    login(client, "user@example.com")
    # b.pdf's page renders identically to a.pdf's -> extraction reused from a.
    upload_pdf(client, course.id, "a.pdf", make_pdf(["Shared page"]))
    upload_pdf(client, course.id, "b.pdf", make_pdf(["Shared page"]))

    doc_b = _document(db, "b.pdf")
    extraction_b = doc_b.latest_version.pages[0].extraction
    assert extraction_b.source == ExtractionSource.REUSED
    assert extraction_b.source_page_id is not None
    markdown = extraction_b.markdown

    doc_a = _document(db, "a.pdf")
    client.post(f"/courses/{course.id}/documents/{doc_a.id}/delete")

    db.expire_all()
    assert _document(db, "a.pdf") is None
    doc_b = _document(db, "b.pdf")
    extraction_b = doc_b.latest_version.pages[0].extraction
    assert extraction_b is not None
    assert extraction_b.markdown == markdown
    assert extraction_b.source == ExtractionSource.REUSED
    assert extraction_b.source_page_id is None  # provenance link cleared


def test_delete_document_removes_its_embeddings(client, db, member, course):
    """Regression: deleting an embedded document used to fail outright.

    `page_embeddings.page_id` had no ON DELETE clause and no call site cleared
    it, so Postgres refused the delete of any document whose pages had been
    embedded — a 500 on a plain "delete this document". The tests missed it
    because SQLite was not enforcing foreign keys at all (see app.db).
    """
    login(client, "user@example.com")
    upload_pdf(client, course.id, "slides.pdf", make_pdf(["Intro", "Processes"]))
    document = _document(db, "slides.pdf")
    assert _count(db, PageEmbedding) > 0  # the delete must have something to trip on

    response = client.post(
        f"/courses/{course.id}/documents/{document.id}/delete",
        follow_redirects=False,
    )
    assert response.status_code == 303

    db.expire_all()
    assert _document(db, "slides.pdf") is None
    # The whole subtree goes with it, leaving nothing orphaned behind.
    assert _count(db, PageEmbedding) == 0
    assert _count(db, PageExtraction) == 0
    assert _count(db, Page) == 0
    assert _count(db, DocumentVersion) == 0


def test_delete_course_leaves_no_orphan_rows(client, db, member, course):
    """The course is the aggregate root: one delete clears every child table."""
    login(client, "user@example.com")
    course_id = course.id
    upload_pdf(client, course_id, "slides.pdf", make_pdf(["Intro", "Processes"]))
    upload_pdf(client, course_id, "syllabus.pdf", make_pdf(["Outline"]))
    assert _count(db, PageEmbedding) > 0

    response = client.post(f"/courses/{course_id}/delete", follow_redirects=False)
    assert response.status_code == 303

    # Read the id back rather than the expired `course` instance, which can no
    # longer refresh itself now that its row is gone.
    db.expunge_all()
    assert db.get(Course, course_id) is None
    for model in (Document, DocumentVersion, Page, PageExtraction, PageEmbedding):
        assert _count(db, model) == 0, model.__name__


def test_delete_version_keeps_the_other_versions(client, db, member, course):
    """One upload of a document can go without taking the document with it."""
    login(client, "user@example.com")
    upload_pdf(client, course.id, "slides.pdf", make_pdf(["v1 only"]))
    upload_pdf(client, course.id, "slides.pdf", make_pdf(["v2 only"]))
    document = _document(db, "slides.pdf")
    assert len(document.versions) == 2
    v1, v2 = document.versions
    v1_dir = storage.abs_path(
        f"courses/{course.id}/documents/{document.id}/v{v1.version_number}"
    )
    v2_dir = storage.abs_path(
        f"courses/{course.id}/documents/{document.id}/v{v2.version_number}"
    )
    assert v1_dir.exists() and v2_dir.exists()
    v1_id, v2_id = v1.id, v2.id

    response = client.post(
        f"/courses/{course.id}/documents/{document.id}/versions/{v1_id}/delete",
        follow_redirects=False,
    )
    assert response.status_code == 303

    db.expire_all()
    document = _document(db, "slides.pdf")
    assert document is not None  # the document survives its deleted version
    assert [v.id for v in document.versions] == [v2_id]
    # Only the deleted version's files go; the survivor's are untouched.
    assert not v1_dir.exists()
    assert v2_dir.exists()
    # And its pages/extractions/embeddings go with it, nothing orphaned.
    assert _count(db, Page) == len(document.versions[0].pages)
    assert db.get(DocumentVersion, v1_id) is None


def test_delete_last_version_deletes_the_document(client, db, member, course):
    """A document with no versions has nothing to show, so the last one takes it."""
    login(client, "user@example.com")
    upload_pdf(client, course.id, "solo.pdf", make_pdf(["Only page"]))
    document = _document(db, "solo.pdf")
    assert len(document.versions) == 1
    version_id = document.versions[0].id
    files_dir = storage.abs_path(f"courses/{course.id}/documents/{document.id}")

    response = client.post(
        f"/courses/{course.id}/documents/{document.id}/versions/{version_id}/delete",
        follow_redirects=False,
    )
    assert response.status_code == 303

    db.expire_all()
    assert _document(db, "solo.pdf") is None
    assert not files_dir.exists()
    assert _count(db, PageEmbedding) == 0
    assert _count(db, Page) == 0


def test_deleted_version_number_is_never_reused(client, db, member, course):
    """A version number names one specific upload, and reports record it.

    Deriving the next number from the surviving rows would hand a deleted v2's
    number to a different PDF, so an old report's "v2" would name a file that
    was never analysed.
    """
    login(client, "user@example.com")
    upload_pdf(client, course.id, "slides.pdf", make_pdf(["first"]))
    upload_pdf(client, course.id, "slides.pdf", make_pdf(["second"]))
    document = _document(db, "slides.pdf")
    assert [v.version_number for v in document.versions] == [1, 2]

    newest = document.versions[-1]
    client.post(
        f"/courses/{course.id}/documents/{document.id}/versions/{newest.id}/delete"
    )
    db.expire_all()

    upload_pdf(client, course.id, "slides.pdf", make_pdf(["third"]))
    db.expire_all()
    document = _document(db, "slides.pdf")
    # v2 was used and removed: the next upload is v3, and the gap stays visible.
    assert [v.version_number for v in document.versions] == [1, 3]


def test_other_users_cannot_delete_a_version(client, db, member, course):
    from app.models import Role, User
    from app.security import hash_password

    login(client, "user@example.com")
    upload_pdf(client, course.id, "a.pdf", make_pdf(["Page one"]))
    upload_pdf(client, course.id, "a.pdf", make_pdf(["Page two"]))
    document = _document(db, "a.pdf")
    version_id = document.versions[0].id

    db.add(
        User(
            email="stranger@example.com",
            password_hash=hash_password("password123"),
            first_name="Stranger",
            last_name="Prof",
            role=Role.USER,
        )
    )
    db.commit()
    other = make_client()
    login(other, "stranger@example.com")
    response = other.post(
        f"/courses/{course.id}/documents/{document.id}/versions/{version_id}/delete"
    )
    assert response.status_code == 404
    db.expire_all()
    assert len(_document(db, "a.pdf").versions) == 2


def test_other_users_cannot_delete(client, db, member, course):
    from app.models import Role, User
    from app.security import hash_password

    login(client, "user@example.com")
    upload_pdf(client, course.id, "a.pdf", make_pdf(["Page"]))
    document = _document(db, "a.pdf")

    db.add(
        User(
            email="other@example.com",
            password_hash=hash_password("password123"),
            first_name="Other",
            last_name="Prof",
            role=Role.USER,
        )
    )
    db.commit()
    other = make_client()
    login(other, "other@example.com")
    response = other.post(f"/courses/{course.id}/documents/{document.id}/delete")
    assert response.status_code == 404
    assert _document(db, "a.pdf") is not None


def test_document_page_offers_exactly_one_delete(client, db, member, course):
    """One destructive button per screen, never two red ones side by side.

    The page used to carry both "delete this version" and "delete the whole
    document", a few pixels apart and both red: the only way to tell them apart
    was to read them. Now a single control removes the version being viewed, and
    the confirmation says whether that takes the document with it.
    """
    login(client, "user@example.com")
    upload_pdf(client, course.id, "one.pdf", make_pdf(["Only page"]))
    document = _document(db, "one.pdf")

    page = client.get(f"/courses/{course.id}/documents/{document.id}")
    assert page.status_code == 200
    assert page.text.count("btn-danger") == 1
    # With one version, the single control removes the document: it posts to
    # the per-version route and the confirmation is scoped to the document.
    assert f"/versions/{document.versions[0].id}/delete" in page.text

    # With several versions, the same single button scopes itself to this one.
    upload_pdf(client, course.id, "one.pdf", make_pdf(["Second upload"]))
    db.expire_all()
    document = _document(db, "one.pdf")
    newest = document.versions[-1]
    page = client.get(
        f"/courses/{course.id}/documents/{document.id}?version_id={newest.id}"
    )
    assert page.text.count("btn-danger") == 1
    # And it points at the per-version route, not the whole-document one.
    assert f"/versions/{newest.id}/delete" in page.text
