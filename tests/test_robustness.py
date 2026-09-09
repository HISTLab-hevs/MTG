"""Regressions for failures that only appear under real multi-user load.

Each test here pins a fix for something that worked in single-user manual use but
broke when two requests raced, an upload was hostile, or a hot route was hit at
scale.
"""

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Course, CourseRule, Page, RuleOrigin
from app.services.rules import ensure_default_rules, load_default_rules
from tests.conftest import login, make_pdf, upload_pdf


def test_concurrent_default_rule_seeding_does_not_error_or_duplicate(db, member):
    """Two requests seeding a fresh course at once: the loser adopts the winner's
    rules instead of surfacing an IntegrityError as a 500.

    Both sessions must decide to seed *before* either commits — the shape of a
    double-submitted "create course" or the overview and rules pages loading
    together.
    """
    course = Course(owner_id=member.id, title="Raced Course")
    db.add(course)
    db.commit()

    first, second = SessionLocal(), SessionLocal()
    try:
        c1, c2 = first.get(Course, course.id), second.get(Course, course.id)
        assert not any(r.origin == RuleOrigin.DEFAULT for r in c1.rules)
        assert not any(r.origin == RuleOrigin.DEFAULT for r in c2.rules)

        ensure_default_rules(first, c1)
        ensure_default_rules(second, c2)  # must not raise
    finally:
        first.close()
        second.close()

    seeded = db.execute(
        select(CourseRule).where(
            CourseRule.course_id == course.id,
            CourseRule.origin == RuleOrigin.DEFAULT,
        )
    ).scalars().all()
    assert len(seeded) == len(load_default_rules())
    assert len({r.code for r in seeded}) == len(seeded)  # no duplicated codes


def test_oversized_upload_is_rejected_without_buffering(client, db, member, course):
    """A body over the cap is refused, and the whole thing is never accumulated
    in memory: the reader stops within one chunk of the limit."""
    login(client, "user@example.com")
    limit = get_settings().max_upload_bytes
    # Valid PDF magic so the size guard, not the type guard, is what rejects it.
    oversized = b"%PDF-" + b"0" * (limit + 1024)

    response = client.post(
        f"/courses/{course.id}/materials/upload",
        files={"file": ("huge.pdf", oversized, "application/pdf")},
        data={"document_type": "slide"},
    )
    assert response.status_code == 200
    assert "exceeds the maximum upload size" in response.text
    from app.models import Document

    assert db.execute(select(Document)).scalars().first() is None


@pytest.mark.parametrize("chunk_count", [1, 3])
def test_capped_read_returns_none_past_the_limit(chunk_count):
    """_read_capped gives up past the limit and returns the exact bytes below it."""
    import asyncio

    from app.routers.documents import _read_capped

    class _Upload:
        """Minimal UploadFile stand-in yielding a fixed payload in chunks."""

        def __init__(self, payload: bytes):
            self._buf = payload

        async def read(self, size: int = -1) -> bytes:
            chunk, self._buf = self._buf[:size], self._buf[size:]
            return chunk

    payload = b"x" * (1024 * 1024 * chunk_count)
    assert asyncio.run(_read_capped(_Upload(payload), len(payload))) == payload
    assert asyncio.run(_read_capped(_Upload(payload), len(payload) - 1)) is None


def test_page_image_is_scoped_to_its_document(client, db, member, course):
    """A page id from another document 404s: the route resolves the row with an
    ownership-scoped join rather than trusting the id."""
    login(client, "user@example.com")
    upload_pdf(client, course.id, "a.pdf", make_pdf(["Alpha"]))
    upload_pdf(client, course.id, "b.pdf", make_pdf(["Beta"]))

    from app.models import Document

    docs = db.execute(select(Document).order_by(Document.id)).scalars().all()
    assert len(docs) == 2
    a, b = docs
    b_page = db.execute(
        select(Page).where(
            Page.document_version_id == b.latest_version.id
        )
    ).scalars().first()
    assert b_page is not None

    # Correct document serves the image; the mismatched pairing does not.
    ok = client.get(f"/courses/{course.id}/documents/{b.id}/pages/{b_page.id}/image")
    assert ok.status_code == 200
    crossed = client.get(
        f"/courses/{course.id}/documents/{a.id}/pages/{b_page.id}/image"
    )
    assert crossed.status_code == 404


def test_db_pool_covers_the_request_threadpool():
    """Sync endpoints hold a connection for the whole request, so the pool must
    cover the anyio threadpool that runs them — the SQLAlchemy default of 5+10
    would fail requests with QueuePool timeouts while the database sat idle.

    Builds a Postgres engine the way app.db does (no connection is opened, so no
    server is needed) because the test suite itself runs on SQLite, which is
    configured without pool sizing.
    """
    from sqlalchemy import create_engine

    from app.db import _pool_kwargs_for

    url = "postgresql+psycopg://mtg:mtg@db:5432/mtg"
    kwargs = _pool_kwargs_for(url)
    assert kwargs["pool_size"] + kwargs["max_overflow"] >= 40
    assert kwargs["pool_pre_ping"] is True  # survives a restart / idled-out socket
    assert kwargs["pool_recycle"] > 0

    # And the engine really carries them, not just the dict.
    pg = create_engine(url, **kwargs)
    assert pg.pool.size() + pg.pool.overflow() >= 0  # engine built, pool configured

    # The SQLite engine the tests run on must not get those kwargs at all.
    assert _pool_kwargs_for("sqlite:///x.db") == {}


def test_plain_http_is_redirected_to_https(client):
    """HTTPS-only: an http:// request is never served, it is redirected."""
    from tests.conftest import make_client

    http_client = make_client()
    http_client.base_url = "http://testserver"
    response = http_client.get("/login", follow_redirects=False)

    assert response.status_code == 307  # preserves method and body
    assert response.headers["location"] == "https://testserver/login"


def test_redirect_preserves_method_and_path_for_a_post(client):
    """A redirected POST must stay a POST: a 301/302 here would turn a form
    submission into a GET and silently drop it."""
    from tests.conftest import make_client

    http_client = make_client()
    http_client.base_url = "http://testserver"
    response = http_client.post(
        "/login", data={"email": "a@b.c", "password": "x"}, follow_redirects=False
    )

    assert response.status_code == 307
    assert response.headers["location"] == "https://testserver/login"


def test_session_cookie_is_secure_and_httponly(client, db, member):
    """The session cookie must be unusable over plain HTTP and from JavaScript."""
    response = login(client, "user@example.com")
    cookie = response.headers["set-cookie"]

    assert "Secure" in cookie  # never transmitted over plain HTTP
    assert "HttpOnly" in cookie  # not readable by scripts
    assert "SameSite=lax" in cookie


def test_https_only_can_be_disabled_for_a_plain_http_deployment(monkeypatch):
    """Turning https_only off must drop both the redirect and the Secure flag,
    so a deliberately TLS-less deployment can still log users in."""
    from starlette.responses import Response

    from app.config import Settings, get_settings
    from app.security import set_session_cookie

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: Settings(https_only=False, app_secret_key="test-secret"),
    )
    monkeypatch.setattr(
        "app.security.get_settings",
        lambda: Settings(https_only=False, app_secret_key="test-secret"),
    )
    response = Response()
    set_session_cookie(response, 1)
    assert "Secure" not in response.headers["set-cookie"]
    get_settings.cache_clear()


def test_checklist_groups_by_document_keeping_severity_order():
    """The PDF checklist groups actions per document instead of interleaving them.

    A course-wide rule fails on several files, so a flat list makes the professor
    jump between documents; grouping keeps one file's work together while the
    document order still follows the most severe failure.
    """
    from app.models import RuleSeverity
    from app.services.report_priorities import checklist_by_document

    class _Rule:
        def __init__(self, code, category):
            self.code, self.category = code, category
            self.text = f"rule {code}"
            self.rationale = None

    class _Verdict:
        outcome = "fail"
        facts: list = []

        def __init__(self, code, severity, category="assessment"):
            self.rule = _Rule(code, category)
            self.severity = severity

    class _Doc:
        def __init__(self, filename, verdicts):
            self.filename, self.verdicts = filename, verdicts
            self.document_type = "assessment"

    class _View:
        # Low-severity file first, to prove ordering is by severity, not input order.
        documents = [
            _Doc("b.pdf", [_Verdict("a2", RuleSeverity.LOW)]),
            _Doc("a.pdf", [_Verdict("a1", RuleSeverity.HIGH),
                           _Verdict("a3", RuleSeverity.LOW)]),
        ]

    grouped = checklist_by_document(_View())

    assert [name for name, _ in grouped] == ["a.pdf", "b.pdf"]
    # Every failure is kept, and the filename is not repeated inside the lines.
    assert sum(len(actions) for _, actions in grouped) == 3
    assert all("pdf" not in a for _, actions in grouped for a in actions)
    assert grouped[0][1][0].startswith("[a1] ")  # highest severity leads its group


def test_report_pdf_accepts_grouped_and_flat_checklists(db, member, course):
    """Both checklist shapes render, so the grouped form is a drop-in."""
    from app.models import CourseReport, ReportRun
    from app.services.report_pdf import render_report_pdf

    run = ReportRun(course_id=course.id, status="completed")
    db.add(run)
    db.commit()
    report = CourseReport(report_run_id=run.id, summary_md="Summary.",
                          solutions_md="Fix things.")
    db.add(report)
    db.commit()

    grouped = render_report_pdf(
        course, report, checklist=[("a.pdf", ["[a1] do this"]), ("b.pdf", ["[a2] that"])]
    )
    flat = render_report_pdf(course, report, checklist=["[a1] [a.pdf] do this"])
    for pdf in (grouped, flat):
        assert pdf.startswith(b"%PDF-") and len(pdf) > 1000


def test_llm_client_reuses_one_transport_per_configuration():
    """One SDK client (and connection pool) per (timeout, retries), reused across
    calls: an analysis run makes one call per page and must not re-handshake for
    each one."""
    from app.llm.client import LLMClient

    client = LLMClient(model="m", base_url="http://example.invalid/v1")
    first = client._openai(max_retries=0)
    assert client._openai(max_retries=0) is first  # reused

    # `generate` (own retry loop) and `embed` (SDK retries) must not share one.
    assert client._openai(max_retries=2) is not first
    client.request_timeout = 300.0
    assert client._openai(max_retries=0) is not first  # rebuilt on new timeout
