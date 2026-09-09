import hashlib
import os
import re
import tempfile
from pathlib import Path

# Test environment must be in place before any app module is imported:
# SQLite instead of Postgres and eager in-process jobs instead of Redis/RQ.
# Tests patch the LLM call explicitly where extraction output is needed.
_TMP = Path(tempfile.mkdtemp(prefix="mtg-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["STORAGE_DIR"] = str(_TMP / "storage")
os.environ["JOB_EAGER"] = "true"
os.environ["APP_SECRET_KEY"] = "test-secret"
os.environ["EXTRACTION_BASE_URL"] = "http://test-vllm.local:8000/v1"
os.environ["EXTRACTION_MODEL"] = "test-extraction-model"
os.environ["GENERATION_BASE_URL"] = "http://test-llm.local:8001/v1"
os.environ["GENERATION_MODEL"] = "test-generation-model"
# Pointed at an unroutable test host, like the other two, so a developer's real
# .env endpoint never leaks in; `embed` itself is stubbed autouse below.
os.environ["EMBEDDING_BASE_URL"] = "http://test-embed.local:8002/v1"
os.environ["EMBEDDING_MODEL"] = "test-embedding-model"

import pymupdf  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import EMBEDDING_DIM, Course, Role, User  # noqa: E402
from app.security import hash_password  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def make_client() -> TestClient:
    """A client speaking https://, the only scheme the app serves.

    The app is HTTPS-only (Settings.https_only): over http:// every request
    answers 307 to its HTTPS equivalent and the Secure session cookie is never
    sent back, so tests must use the same scheme production does. Build clients
    through here rather than calling TestClient directly, so the scheme is
    defined in one place.
    """
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def client():
    return make_client()


STUB_RULE_ANSWER = (
    '{"description": "Stubbed rule.",'
    ' "rationale": "Stubbed rationale.",'
    ' "cel": "size(doc.exam_dates) > 0",'
    ' "severity": "medium", "category": "assessment",'
    ' "contract": [{"name": "exam_dates", '
    ' "type": "string_list", "agg": "union",'
    ' "instruction": "List the exam dates on this page."}]}'
)


@pytest.fixture(autouse=True)
def _stub_llm_generate(monkeypatch):
    """Vision calls (with an image) return extraction markdown; text calls
    return a valid rule-generation answer. Tests override where needed."""

    def generate(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        if image_png:
            # page_extraction.j2 tells the model to open with this anchor, so
            # the stub emits it too: page traceability is asserted downstream.
            page = re.search(r"<!-- page: (\d+) -->", prompt or "")
            anchor = f"<!-- page: {page.group(1)} -->\n\n" if page else ""
            return f"{anchor}stubbed extraction markdown"
        return STUB_RULE_ANSWER

    monkeypatch.setattr("app.llm.client.LLMClient.generate", generate)


@pytest.fixture(autouse=True)
def _stub_llm_embed(monkeypatch):
    """Deterministic unit vectors, so processing reaches READY without a server.

    Derived from the text's hash rather than constant: identical text embeds
    identically and different text does not, which is what retrieval ordering
    depends on. Tests that assert on specific neighbours override this.
    """

    def embed(self, inputs):
        vectors = []
        for text in inputs:
            seed = hashlib.sha256(text.encode()).digest()
            vectors.append([
                seed[i % len(seed)] / 255.0 for i in range(EMBEDDING_DIM)
            ])
        return vectors

    monkeypatch.setattr("app.llm.client.LLMClient.embed", embed)


def _create_user(db, email, role):
    user = User(
        email=email,
        password_hash=hash_password("password123"),
        first_name="Test",
        last_name="User",
        role=role,
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def admin(db):
    return _create_user(db, "admin@example.com", Role.ADMIN)


@pytest.fixture
def member(db):
    return _create_user(db, "user@example.com", Role.USER)


@pytest.fixture
def course(db, member):
    course = Course(owner_id=member.id, title="Operating Systems")
    db.add(course)
    db.commit()
    return course


def login(client, email, password="password123"):
    return client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )


def make_pdf(page_texts: list[str]) -> bytes:
    """Build a small deterministic PDF, one page per text."""
    doc = pymupdf.open()
    for text in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def upload_pdf(
    client,
    course_id: int,
    filename: str,
    data: bytes,
    document_type: str | list[str] = "slide",
):
    # A document may carry several types; httpx sends a list value as repeated
    # form fields, which FastAPI collects back into a list[str].
    return client.post(
        f"/courses/{course_id}/materials/upload",
        files={"file": (filename, data, "application/pdf")},
        data={"document_type": document_type},
    )
