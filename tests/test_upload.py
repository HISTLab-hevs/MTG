"""Upload validation, document versioning, and the page-level version diff.

A re-upload of the same filename becomes a new version; the diff against the
previous one is computed from page image hashes, not from the text.
"""

import hashlib

from sqlalchemy import select

from app.models import Document, DocumentVersion
from app.services import storage
from tests.conftest import login, make_pdf, upload_pdf


def test_non_pdf_upload_is_rejected(client, db, member, course):
    login(client, "user@example.com")
    response = client.post(
        f"/courses/{course.id}/materials/upload",
        files={"file": ("notes.txt", b"plain text notes", "text/plain")},
        data={"document_type": "slide"},
    )
    assert response.status_code == 200
    assert "convert your course material" in response.text
    assert db.execute(select(Document)).scalars().first() is None


def test_pdf_with_wrong_magic_bytes_is_rejected(client, db, member, course):
    login(client, "user@example.com")
    response = client.post(
        f"/courses/{course.id}/materials/upload",
        files={"file": ("fake.pdf", b"not a real pdf", "application/pdf")},
        data={"document_type": "slide"},
    )
    assert "Only PDF files are accepted" in response.text
    assert db.execute(select(Document)).scalars().first() is None


def test_valid_pdf_upload_creates_versioned_document(client, db, member, course):
    login(client, "user@example.com")
    data = make_pdf(["Syllabus page one"])
    upload_pdf(client, course.id, "syllabus.pdf", data)

    document = db.execute(select(Document)).scalar_one()
    assert document.filename == "syllabus.pdf"
    assert document.document_type == "slide"
    version = db.execute(select(DocumentVersion)).scalar_one()
    assert version.version_number == 1
    assert version.file_sha256 == hashlib.sha256(data).hexdigest()
    assert storage.abs_path(version.file_path).read_bytes() == data


def test_upload_with_multiple_document_types(client, db, member, course):
    login(client, "user@example.com")
    upload_pdf(
        client, course.id, "exam.pdf", make_pdf(["Q1"]),
        document_type=["rubric", "assessment"],
    )

    document = db.execute(select(Document)).scalar_one()
    # Stored in canonical ALL order regardless of how they were submitted.
    assert document.document_type == "assessment,rubric"
    assert document.types == ["assessment", "rubric"]


def test_upload_dedupes_and_drops_unknown_types(client, db, member, course):
    login(client, "user@example.com")
    upload_pdf(
        client, course.id, "x.pdf", make_pdf(["x"]),
        document_type=["slide", "slide", "poster"],
    )

    document = db.execute(select(Document)).scalar_one()
    assert document.document_type == "slide"


def test_upload_with_invalid_document_type_is_rejected(client, db, member, course):
    login(client, "user@example.com")
    response = upload_pdf(
        client, course.id, "x.pdf", make_pdf(["x"]), document_type="poster"
    )
    assert "at least one document type" in response.text
    assert db.execute(select(Document)).scalars().first() is None


def test_identical_pdf_upload_is_not_reprocessed(client, db, member, course):
    login(client, "user@example.com")
    data = make_pdf(["Syllabus page one"])
    upload_pdf(client, course.id, "syllabus.pdf", data)
    response = upload_pdf(client, course.id, "syllabus.pdf", data)

    assert "already uploaded" in response.text
    versions = db.execute(select(DocumentVersion)).scalars().all()
    assert len(versions) == 1


def test_modified_pdf_creates_second_version(client, db, member, course):
    login(client, "user@example.com")
    upload_pdf(client, course.id, "syllabus.pdf", make_pdf(["Original content"]))
    upload_pdf(client, course.id, "syllabus.pdf", make_pdf(["Updated content"]))

    document = db.execute(select(Document)).scalar_one()
    assert [v.version_number for v in document.versions] == [1, 2]


def test_version_diff_reports_page_changes_from_image_hashes():
    """The page-level diff vs. the previous version is computed from image hashes:
    same hash = unchanged, different = changed, and added/removed by page number."""
    from types import SimpleNamespace

    from app.routers.documents import _version_diff

    def page(n, h):
        return SimpleNamespace(page_number=n, image_hash=h)

    v1 = SimpleNamespace(
        id=1, version_number=1,
        pages=[page(1, "a"), page(2, "b"), page(3, "c")],
    )
    v2 = SimpleNamespace(
        id=2, version_number=2,
        # page 1 unchanged, page 2 changed, page 3 removed, page 4 added.
        pages=[page(1, "a"), page(2, "B2"), page(4, "d")],
    )
    document = SimpleNamespace(versions=[v1, v2])

    assert _version_diff(document, v1) is None  # first version: no prior to diff
    diff = _version_diff(document, v2)
    assert diff["prev_number"] == 1
    assert diff["changed"] == [2]
    assert diff["added"] == [4]
    assert diff["removed"] == [3]
    assert diff["unchanged"] == 1
    assert diff["has_changes"] is True


def test_version_diff_for_third_version_compares_against_second():
    """With three versions the diff for v3 is against v2 (the immediately previous
    one), NOT v1 — so the professor sees what changed in their latest edit, page by
    page, relative to the version they were last working from."""
    from types import SimpleNamespace

    from app.routers.documents import _previous_version, _version_diff

    def page(n, h):
        return SimpleNamespace(page_number=n, image_hash=h)

    v1 = SimpleNamespace(id=1, version_number=1,
                         pages=[page(1, "a"), page(2, "b"), page(3, "c")])
    v2 = SimpleNamespace(id=2, version_number=2,
                         pages=[page(1, "a"), page(2, "B2"), page(3, "c")])
    # v3: page 3 changed since v2; pages 1 and 2 identical to v2.
    v3 = SimpleNamespace(id=3, version_number=3,
                         pages=[page(1, "a"), page(2, "B2"), page(3, "C3")])
    document = SimpleNamespace(versions=[v1, v2, v3])

    # The "previous" of v3 is v2, not v1.
    assert _previous_version(document, v3) is v2

    diff = _version_diff(document, v3)
    assert diff["prev_number"] == 2          # compared to v2
    assert diff["changed"] == [3]            # only page 3 changed since v2
    assert diff["added"] == []
    assert diff["removed"] == []
    assert diff["unchanged"] == 2            # pages 1 and 2
    assert diff["has_changes"] is True

    # And v2's own diff is still against v1 (page 2 changed there).
    assert _version_diff(document, v2)["prev_number"] == 1
    assert _version_diff(document, v2)["changed"] == [2]


def test_users_cannot_touch_other_users_courses(client, db, admin, member, course):
    from app.models import Role, User
    from app.security import hash_password

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

    login(client, "other@example.com")
    assert client.get(f"/courses/{course.id}/overview").status_code == 404
    response = upload_pdf(client, course.id, "x.pdf", make_pdf(["x"]))
    assert response.status_code == 404


def test_polled_panels_carry_disclosure_keys(client, db, member, course):
    """Panels that poll must let their open sections survive the swap.

    While a PDF is processing, the material list re-fetches itself every few
    seconds and REPLACES its subtree, so a version history the professor opened
    becomes a fresh, closed element, snapping shut under them while they read.
    app.js restores any <details data-key> after a swap; that only works if the
    markup actually carries the key, which is what this pins down.
    """
    login(client, "user@example.com")
    upload_pdf(client, course.id, "slides.pdf", make_pdf(["Intro"]))

    page = client.get(f"/courses/{course.id}/materials")
    assert page.status_code == 200
    # The disclosure holding the version list is keyed...
    assert 'class="doc-row-details" data-key="doc-' in page.text
    # ...and the fragment the poll swaps in carries the same key, so the state
    # can be matched across the swap.
    fragment = client.get(f"/courses/{course.id}/materials/list")
    assert fragment.status_code == 200
    assert 'data-key="doc-' in fragment.text
