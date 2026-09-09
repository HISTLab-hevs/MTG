"""Page embedding and retrieval for material RAG.

Embeddings are produced as the last step of document processing (one vector per
page, from the page's extracted Markdown) and stored in pgvector. Retrieval is a
single cosine-distance query, filtered to the course, so the materials chat can
ground its answers in the most relevant pages.

The index holds exactly one generation of each document, the version in force. A
new version's vectors replace its predecessor's (_drop_superseded_embeddings)
rather than joining them, so the ranking never spends its k slots on editions the
professor has already replaced. Pages, extractions and the original PDFs of every
version are kept regardless: only the vectors are pruned.

The embedding endpoint is required, like the extraction and generation ones: a
document that cannot be embedded fails processing rather than reaching READY
with no vectors, which would leave it permanently invisible to the chat.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import delete, select, tuple_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm.client import get_embedding_client
from app.models import (
    Document,
    DocumentType,
    DocumentVersion,
    Page,
    PageEmbedding,
    PageExtraction,
    PageStatus,
)

logger = logging.getLogger(__name__)


@dataclass
class RetrievedPage:
    """One page returned by retrieval, ready to be quoted in an answer."""

    filename: str
    page_number: int
    markdown: str


def _embedding_text(document: Document, page_number: int, markdown: str) -> str:
    """The text indexed for a page: its document identity plus the page body.

    Prefixing the filename, document type and page number lets a query about a
    topic the document is about, an "Isolation" lecture for instance, match on
    identity and not only on whichever page mentions the word most densely. This
    is a general recall improvement, independent of any specific course.

    Args:
        document: Document the page belongs to.
        page_number: Its 1-based number.
        markdown: The page's extracted text.

    Returns:
        The text to embed.
    """
    return (
        f"Document: {document.filename} (type: {DocumentType.label(document.document_type)}), "
        f"page {page_number}\n\n{markdown}"
    )


def embed_version_pages(db: Session, version: DocumentVersion) -> None:
    """Embed every ready page of a version, replacing the older vectors.

    Commits per batch, so progress survives a mid-run failure.

    Args:
        db: Session the vectors are written through.
        version: Version whose pages to embed.

    Raises:
        RuntimeError: No embedding endpoint is configured. Processing treats
            that as a failed document rather than letting it reach READY with no
            vectors, which would leave the chat unable to retrieve it.
    """
    client = get_embedding_client()
    if not client.is_configured:
        raise RuntimeError(
            "Page embedding requires EMBEDDING_BASE_URL and EMBEDDING_MODEL."
        )

    pages = [p for p in version.pages if p.status == PageStatus.READY and p.extraction]
    if not pages:
        return

    document = version.document
    course_id = document.course_id
    db.execute(
        delete(PageEmbedding).where(
            PageEmbedding.page_id.in_([p.id for p in pages])
        )
    )
    _drop_superseded_embeddings(db, version)

    batch_size = max(1, get_settings().embedding_batch_size)
    for start in range(0, len(pages), batch_size):
        batch = pages[start : start + batch_size]
        vectors = client.embed(
            [_embedding_text(document, p.page_number, p.extraction.markdown) for p in batch]
        )
        db.add_all(
            PageEmbedding(
                page_id=page.id,
                course_id=course_id,
                model_name=client.model,
                embedding=vector,
            )
            for page, vector in zip(batch, vectors)
        )
        db.commit()


def _drop_superseded_embeddings(db: Session, version: DocumentVersion) -> None:
    """Delete the vectors of this document's earlier versions.

    The index holds one generation per document, the one in force. Keeping the
    old ones would put several versions of the same page in the same
    cosine-distance ranking, and the damage is not that the chat might cite a
    stale page, it is that the k slots are spent on it. On a course whose
    documents have been revised twice, two thirds of the ranked rows are
    superseded copies, so the slots meant for the k most relevant pages get
    filled by older editions of pages already retrieved, and the answer loses
    the material that would have ranked next. Filtering them out in retrieve
    would still sort every generation on every question, and would be invisible
    to a future HNSW index; deleting keeps the searched set exactly as large as
    what can legitimately answer.

    Nothing else is lost: pages, extractions and the original PDF of every
    version stay untouched, the history reports compare is built from those
    rather than from vectors, and a superseded version that is ever needed again
    can be re-embedded from its stored Markdown.

    Called only once the replacing vectors are being written, and a version that
    fails never reaches this step, since embedding is the last one (see
    processing.process_document_version). A document is therefore never left
    with no vectors at all: either the new generation lands or the previous one
    stands.

    Args:
        db: Session to execute the delete on.
        version: Version whose predecessors to prune.
    """
    superseded = (
        select(Page.id)
        .join(DocumentVersion, DocumentVersion.id == Page.document_version_id)
        .where(
            DocumentVersion.document_id == version.document_id,
            DocumentVersion.id != version.id,
        )
    )
    db.execute(
        delete(PageEmbedding).where(PageEmbedding.page_id.in_(superseded))
    )


def _expand_with_neighbours(
    hits: list[tuple[int, int]]
) -> set[tuple[int, int]]:
    """Widen retrieval hits to include each page's immediate neighbours.

    Page numbers are 1-based, so page 0 is never requested. Kept separate from
    the query so the window behaviour is testable without a database.

    Args:
        hits: The (version, page) pairs cosine retrieval returned.

    Returns:
        Those pairs plus the page before and after each.
    """
    wanted: set[tuple[int, int]] = set()
    for version_id, page_number in hits:
        for n in (page_number - 1, page_number, page_number + 1):
            if n >= 1:
                wanted.add((version_id, n))
    return wanted


def retrieve(
    db: Session, course_id: int, query: str, k: int | None = None
) -> list[RetrievedPage]:
    """The course's most relevant pages, with their adjacent pages.

    A relevant passage often spills across page boundaries, a definition started
    on one slide and finished on the next. Cosine retrieval scores pages
    independently, so it can surface page 5 while the sentence completing the
    answer sits on page 4 or 6. Each hit's immediate neighbours are therefore
    pulled in. They are fetched by page number, not embedded, so this adds one
    light query and never removes a retrieved page.

    Args:
        db: Session to query.
        course_id: Course to search within.
        query: Text to embed and rank against.
        k: How many pages to rank before widening; defaults to the configured
            materials_retrieval_k.

    Returns:
        The pages ordered by document then page number, so passages read in
        sequence.

    Raises:
        RuntimeError: No embedding endpoint is configured. Callers running
            inline in a web request check is_configured first (see
            materials_chat.run_materials_chat) so the user gets a message, not a
            500.
    """
    client = get_embedding_client()
    if not client.is_configured:
        raise RuntimeError(
            "Page retrieval requires EMBEDDING_BASE_URL and EMBEDDING_MODEL."
        )
    if k is None:
        k = get_settings().materials_retrieval_k
    query_vector = client.embed([query])[0]

    hits = db.execute(
        select(Page.document_version_id, Page.page_number)
        .select_from(PageEmbedding)
        .join(Page, Page.id == PageEmbedding.page_id)
        .where(PageEmbedding.course_id == course_id)
        .order_by(PageEmbedding.embedding.cosine_distance(query_vector))
        .limit(k)
    ).all()
    if not hits:
        return []

    wanted = _expand_with_neighbours(hits)

    rows = db.execute(
        select(
            Document.filename,
            Page.page_number,
            PageExtraction.markdown,
            Page.document_version_id,
        )
        .select_from(Page)
        .join(PageExtraction, PageExtraction.page_id == Page.id)
        .join(DocumentVersion, DocumentVersion.id == Page.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            tuple_(Page.document_version_id, Page.page_number).in_(wanted)
        )
    ).all()

    rows.sort(key=lambda r: (r.filename, r.document_version_id, r.page_number))
    return [
        RetrievedPage(r.filename, r.page_number, r.markdown)
        for r in rows
    ]
