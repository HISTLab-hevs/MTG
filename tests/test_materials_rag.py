"""Material RAG: page embedding in the pipeline + the read-only chat endpoint.

conftest configures the embedding endpoint and stubs `embed` autouse, so
processing reaches READY without a server. Retrieval itself needs pgvector's
distance operator, which SQLite lacks, so tests that exercise it stub
`retrieve` rather than issuing the query.
"""

import pytest

from app.models import EMBEDDING_DIM, PageEmbedding
from app.services import embedding as embedding_service
from app.services.embedding import RetrievedPage

from tests.conftest import login, make_pdf, upload_pdf


def _process_doc(client, db, course, monkeypatch, pages=("Week 1", "Week 2")):
    """Upload and process a PDF, returning its version id."""
    login(client, "user@example.com")
    upload_pdf(client, course.id, "syllabus.pdf", make_pdf(list(pages)))
    from app.models import DocumentVersion

    return db.query(DocumentVersion).order_by(DocumentVersion.id.desc()).first()


class _StubEmbedder:
    """Stand-in embedding client: always configured, returns unit vectors."""

    model = "stub-embed"
    is_configured = True

    def embed(self, inputs):
        return [[0.0] * (EMBEDDING_DIM - 1) + [1.0] for _ in inputs]


def test_embedding_required_when_unconfigured(client, db, course, monkeypatch):
    """With no embedding endpoint, embedding fails loudly instead of no-opping.

    A silent skip would let the document reach READY with no vectors, leaving it
    permanently invisible to the materials chat.
    """
    version = _process_doc(client, db, course, monkeypatch)

    class _Unconfigured:
        is_configured = False

    monkeypatch.setattr(
        "app.services.embedding.get_embedding_client", lambda: _Unconfigured()
    )
    db.refresh(version)
    with pytest.raises(RuntimeError, match="EMBEDDING_BASE_URL"):
        embedding_service.embed_version_pages(db, version)


def test_embed_version_pages_writes_one_vector_per_page(
    client, db, course, monkeypatch
):
    """A configured embedder yields exactly one embedding per READY page."""
    version = _process_doc(client, db, course, monkeypatch)

    monkeypatch.setattr(
        "app.services.embedding.get_embedding_client",
        lambda: _StubEmbedder(),
    )
    db.refresh(version)
    embedding_service.embed_version_pages(db, version)

    rows = db.query(PageEmbedding).all()
    assert len(rows) == len(version.pages)
    assert all(r.course_id == course.id for r in rows)
    assert all(len(r.embedding) == EMBEDDING_DIM for r in rows)


def test_embed_is_idempotent(client, db, course, monkeypatch):
    """Re-embedding replaces vectors rather than duplicating them."""
    version = _process_doc(client, db, course, monkeypatch)
    monkeypatch.setattr(
        "app.services.embedding.get_embedding_client", lambda: _StubEmbedder()
    )
    db.refresh(version)
    embedding_service.embed_version_pages(db, version)
    embedding_service.embed_version_pages(db, version)
    assert db.query(PageEmbedding).count() == len(version.pages)


def test_document_ready_only_after_embedding(client, db, course, monkeypatch):
    """With an embedder configured, a processed version is READY and indexed."""
    from app.models import DocumentStatus

    monkeypatch.setattr(
        "app.services.embedding.get_embedding_client", lambda: _StubEmbedder()
    )
    login(client, "user@example.com")
    upload_pdf(client, course.id, "syllabus.pdf", make_pdf(["Week 1", "Week 2"]))

    from app.models import DocumentVersion

    version = db.query(DocumentVersion).order_by(DocumentVersion.id.desc()).first()
    assert version.status == DocumentStatus.READY
    assert db.query(PageEmbedding).count() == len(version.pages)


def test_embedding_failure_fails_the_document(client, db, course, monkeypatch):
    """If embedding raises, the version is FAILED, not a half-indexed READY."""
    from app.models import DocumentStatus, DocumentVersion

    class _Boom:
        model = "boom"
        is_configured = True

        def embed(self, inputs):
            raise RuntimeError("embed endpoint down")

    monkeypatch.setattr(
        "app.services.embedding.get_embedding_client", lambda: _Boom()
    )
    login(client, "user@example.com")
    upload_pdf(client, course.id, "syllabus.pdf", make_pdf(["Week 1"]))

    version = db.query(DocumentVersion).order_by(DocumentVersion.id.desc()).first()
    assert version.status == DocumentStatus.FAILED
    assert db.query(PageEmbedding).count() == 0


def test_materials_page_has_chat_starters_and_new_chat(client, course, member):
    """The materials chat is signposted with starter chips and offers a New chat
    control to clear the conversation and start over."""
    login(client, "user@example.com")
    page = client.get(f"/courses/{course.id}/materials")
    assert page.status_code == 200
    body = page.text
    assert 'class="chat-starter materials-starter"' in body
    assert 'id="materials-chat-new"' in body
    assert 'data-max-chars="16000"' in body


def test_materials_chat_without_context(client, course, monkeypatch):
    """No retrieved pages → the assistant says it found nothing, no model call.

    Retrieval is stubbed because the real query needs pgvector, absent under the
    tests' SQLite; the case under test is an empty result, not the SQL.
    """
    monkeypatch.setattr(
        "app.services.materials_chat.retrieve",
        lambda db, course_id, message, k=None: [],
    )
    login(client, "user@example.com")
    resp = client.post(
        f"/courses/{course.id}/materials/chat",
        data={"message": "How long is the course?", "history": "[]"},
    )
    assert resp.status_code == 200
    assert "find anything in this course" in resp.text.lower()


def test_materials_chat_reports_unconfigured_embedder(client, course, monkeypatch):
    """A missing embedder is reported as such, not as "nothing found".

    Running inline in the request, it must not 500; and conflating it with an
    empty result would hide a broken deployment behind a normal-looking answer.
    """

    class _Unconfigured:
        is_configured = False

    monkeypatch.setattr(
        "app.services.materials_chat.get_embedding_client", lambda: _Unconfigured()
    )
    login(client, "user@example.com")
    resp = client.post(
        f"/courses/{course.id}/materials/chat",
        data={"message": "How long is the course?", "history": "[]"},
    )
    assert resp.status_code == 200
    assert "no embedding model is configured" in resp.text.lower()


def test_materials_chat_grounded_answer(client, course, monkeypatch):
    """Numbered citations become chips, and only cited pages list as sources."""
    monkeypatch.setattr(
        "app.services.materials_chat.retrieve",
        lambda db, course_id, message, k=6: [
            RetrievedPage("syllabus.pdf", 2, "The course lasts 6 weeks."),
            RetrievedPage("slides.pdf", 5, "Unrelated page."),
        ],
    )
    monkeypatch.setattr(
        "app.llm.client.LLMClient.generate",
        lambda self, prompt, **kw: "The course lasts **6 weeks** [1].",
    )
    login(client, "user@example.com")
    resp = client.post(
        f"/courses/{course.id}/materials/chat",
        data={"message": "How long is the course?", "history": "[]"},
    )
    assert resp.status_code == 200
    # Markdown is rendered, the [1] became a citation chip...
    assert "<strong>6 weeks</strong>" in resp.text
    assert 'class="cite"' in resp.text
    # ...and only the cited source (1) appears in the footer, not the uncited one.
    assert "syllabus.pdf" in resp.text
    assert "slides.pdf" not in resp.text


def test_retrieval_query_folds_in_last_turn_for_a_short_follow_up():
    """A bare follow-up ("testnet?") has no topic of its own for embedding
    search; the last exchange must be folded in so retrieval still finds the
    right pages instead of near-random ones."""
    from app.services.materials_chat import _retrieval_query

    history = [
        {"role": "user", "content": "What does the syllabus say about grading?"},
        {"role": "assistant", "content": "Grading is based on a final exam [1]."},
    ]
    query = _retrieval_query("and the final exam?", history)
    assert "final exam" in query  # topic from the prior turn carried forward
    assert query.endswith("and the final exam?")  # current message still present


def test_retrieval_query_is_unchanged_without_history():
    from app.services.materials_chat import _retrieval_query

    assert _retrieval_query("How long is the course?", None) == "How long is the course?"
    assert _retrieval_query("How long is the course?", []) == "How long is the course?"


def test_materials_chat_uses_history_aware_retrieval_query(client, course, monkeypatch):
    """The endpoint must pass history through to retrieval, not just the model
    prompt — otherwise a follow-up question retrieves the wrong pages entirely."""
    seen = {}

    def fake_retrieve(db, course_id, message, k=6):
        seen["message"] = message
        return [RetrievedPage("slides.pdf", 20, "TestNet and MainNet deploy.")]

    monkeypatch.setattr("app.services.materials_chat.retrieve", fake_retrieve)
    monkeypatch.setattr(
        "app.llm.client.LLMClient.generate",
        lambda self, prompt, **kw: "Yes, deploy targets TestNet then MainNet [1].",
    )
    login(client, "user@example.com")
    history = [
        {"role": "user", "content": "sulle decentralized applications?"},
        {"role": "assistant", "content": "...deploy finale su TestNet e MainNet."},
    ]
    import json

    resp = client.post(
        f"/courses/{course.id}/materials/chat",
        data={
            "message": "no mi hai citato tu il deploy sulla testnet",
            "history": json.dumps(history),
        },
    )
    assert resp.status_code == 200
    assert "TestNet" in seen["message"]  # topic carried into the retrieval query
    assert "no mi hai citato tu il deploy sulla testnet" in seen["message"]


def test_citations_renumbered_from_one():
    """Model cites raw retrieval indices [4][5]; output shows compact [1][2]."""
    from app.services.materials_chat import _build_result

    passages = [RetrievedPage(f"doc{i}.pdf", i, f"page {i}") for i in range(1, 7)]
    raw = "Isolation matters [5]. Monitoring too [4]."
    result = _build_result(raw, passages)

    html = str(result.reply_html)
    # The raw 5/4 are remapped to display 1/2 in first-citation order.
    assert ">1</sup>" in html and ">2</sup>" in html
    assert ">4</sup>" not in html and ">5</sup>" not in html
    # Sources list carries the display numbers and the right pages.
    assert {(s.number, s.page_number) for s in result.sources} == {(1, 5), (2, 4)}


def test_materials_chat_degrades_gracefully_when_model_unavailable(
    client, course, monkeypatch
):
    """If the model is unreachable (after the client's retries), the chat returns
    a calm retryable message rather than a 500 / broken bubble."""
    monkeypatch.setattr(
        "app.services.materials_chat.retrieve",
        lambda db, course_id, message, k=6: [RetrievedPage("a.pdf", 1, "x")],
    )

    def boom(self, prompt, image_png=None, temperature=0.0, extra_body=None):
        raise RuntimeError("model endpoint unreachable")

    monkeypatch.setattr("app.llm.client.LLMClient.generate", boom)
    login(client, "user@example.com")
    resp = client.post(
        f"/courses/{course.id}/materials/chat",
        data={"message": "anything", "history": "[]"},
    )
    assert resp.status_code == 200
    assert "temporarily unavailable" in resp.text.lower()


def test_materials_chat_degrades_gracefully_when_retrieval_fails(monkeypatch):
    from app.services.materials_chat import run_materials_chat

    class _Configured:
        is_configured = True

    monkeypatch.setattr(
        "app.services.materials_chat.get_generation_client", lambda: _Configured()
    )
    monkeypatch.setattr(
        "app.services.materials_chat.get_embedding_client", lambda: _Configured()
    )
    monkeypatch.setattr(
        "app.services.materials_chat.retrieve",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("embedder down")),
    )
    result = run_materials_chat(None, 1, "What is assessed?", [])
    assert "temporarily unavailable" in str(result.reply_html).lower()


def test_materials_chat_rejects_uncited_or_invalidly_cited_answers():
    from app.services.materials_chat import _build_result

    passages = [RetrievedPage("syllabus.pdf", 1, "The course lasts six weeks.")]
    for raw in ("The course lasts six weeks.", "The course lasts six weeks [99]."):
        result = _build_result(raw, passages)
        assert not result.sources
        assert "source-backed" in str(result.reply_html)
        assert "six weeks" not in str(result.reply_html)


def test_materials_chat_rejects_a_message_over_character_limit():
    from app.llm.prompts import CHAT_MESSAGE_MAX_CHARS
    from app.services.materials_chat import run_materials_chat

    result = run_materials_chat(
        None, 1, "x" * (CHAT_MESSAGE_MAX_CHARS + 1), []
    )
    assert "too long" in str(result.reply_html).lower()
    assert "16,000 characters" in str(result.reply_html)


def test_retrieve_expands_each_hit_with_its_neighbours():
    """Each retrieved page pulls in its ±1 neighbours of the same version, so a
    passage spanning page boundaries reaches the model whole; page 0 is never
    requested, and overlapping windows are de-duplicated."""
    from app.services.embedding import _expand_with_neighbours

    # Two hits in version 10 (pages 1 and 5) and one in version 20 (page 3).
    hits = [(10, 1), (10, 5), (20, 3)]
    wanted = _expand_with_neighbours(hits)

    # Page 1's window is {1,2} (no page 0); page 5's is {4,5,6}; v20 page 3 -> {2,3,4}.
    assert (10, 0) not in wanted  # never below page 1
    assert {(10, 1), (10, 2)} <= wanted
    assert {(10, 4), (10, 5), (10, 6)} <= wanted
    assert {(20, 2), (20, 3), (20, 4)} <= wanted
    # Neighbours of one version never bleed into another.
    assert (20, 5) not in wanted and (10, 3) not in wanted


def test_new_version_replaces_the_previous_version_vectors(client, db, course):
    """The index holds one generation per document, not one per version.

    Keeping both would put two editions of the same page in one ranking and
    spend the retrieval's LIMIT k on text the professor has already replaced.
    """
    from app.models import DocumentVersion

    login(client, "user@example.com")
    upload_pdf(client, course.id, "syllabus.pdf", make_pdf(["Exam is worth 100%"]))
    upload_pdf(client, course.id, "syllabus.pdf", make_pdf(["Exam is worth 60%"]))

    db.expire_all()
    versions = (
        db.query(DocumentVersion).order_by(DocumentVersion.version_number).all()
    )
    assert len(versions) == 2  # history itself is kept

    embedded = {
        v.version_number
        for v in versions
        for p in v.pages
        if db.query(PageEmbedding).filter(PageEmbedding.page_id == p.id).count()
    }
    assert embedded == {2}, "only the version in force stays indexed"
    assert db.query(PageEmbedding).count() == 1

    # The superseded version keeps everything except its vectors, so it stays
    # readable for comparison and could be re-embedded from its own Markdown.
    # (The extraction text itself is stubbed in these tests; what matters here
    # is that the row survived the pruning.)
    old_page = versions[0].pages[0]
    assert old_page.extraction is not None
    assert old_page.extraction.markdown


def test_superseded_vectors_of_other_documents_are_untouched(client, db, course):
    """Pruning is scoped to the document being replaced, not the course."""
    login(client, "user@example.com")
    upload_pdf(client, course.id, "a.pdf", make_pdf(["Alpha"]))
    upload_pdf(client, course.id, "b.pdf", make_pdf(["Beta"]))
    upload_pdf(client, course.id, "a.pdf", make_pdf(["Alpha revised"]))

    db.expire_all()
    # a.pdf: one generation (v2). b.pdf: its only version, still indexed.
    assert db.query(PageEmbedding).count() == 2


def test_embedded_text_includes_document_identity(client, db, course, monkeypatch):
    """Pages are indexed with their filename/type/page, not just the body."""
    version = _process_doc(client, db, course, monkeypatch)
    captured = []

    class _Capturing(_StubEmbedder):
        def embed(self, inputs):
            captured.extend(inputs)
            return super().embed(inputs)

    monkeypatch.setattr(
        "app.services.embedding.get_embedding_client", lambda: _Capturing()
    )
    db.refresh(version)
    embedding_service.embed_version_pages(db, version)

    assert captured, "no text was embedded"
    assert all("Document: syllabus.pdf" in t for t in captured)
    assert all("type: Slides" in t for t in captured)  # upload_pdf default type, labelled


def test_materials_chat_compact_endpoint_returns_payload(client, course, member, monkeypatch):
    import json
    from types import SimpleNamespace

    login(client, "user@example.com")

    def fake_compact(history, chat_kind):
        assert chat_kind == "materials"
        assert history[0]["role"] == "user"
        return SimpleNamespace(as_payload=lambda: {
            "compacted": True,
            "history": [{"role": "summary", "content": "short materials memory"}],
            "compacted_until_at": 456,
            "message": "ok",
        })

    monkeypatch.setattr("app.routers.courses.compact_history", fake_compact)
    resp = client.post(
        f"/courses/{course.id}/materials/chat/compact",
        data={"history": json.dumps([{"role": "user", "content": "hello", "at": 456}])},
    )

    assert resp.status_code == 200
    assert resp.json()["compacted"] is True
    assert resp.json()["history"][0]["content"] == "short materials memory"


def test_empty_chat_message_is_a_no_op_not_an_error(client, db, member, course):
    """Pressing Send on an empty box must change nothing.

    The endpoint's body is swapped straight into the conversation log, so a
    required-field rejection put FastAPI's raw validation JSON
    ({"detail":[{"type":"missing"...}]}) in the chat as if the assistant had
    said it. An empty send is simply nothing to do: 204, which htmx is
    configured to swap nothing for.
    """
    login(client, "user@example.com")
    for body in ({}, {"message": ""}, {"message": "   "}, {"message": "\n\t "}):
        r = client.post(f"/courses/{course.id}/materials/chat", data=body)
        assert r.status_code == 204, body
        assert "detail" not in r.text
        assert r.text.strip() == ""
