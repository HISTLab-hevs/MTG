"""Document and version deletion.

Deleting a logical document removes all its versions, pages, extractions,
embeddings and stored files. The row cascade is the database's: every foreign
key down the chain is ON DELETE CASCADE, so one DELETE clears the subtree.

Extractions of other documents may reference this document's pages through
source_page_id, from hash-based reuse; that key is ON DELETE SET NULL, so the
copied Markdown survives with its provenance link cleared, and no bookkeeping is
needed here.
"""

import shutil

from sqlalchemy.orm import Session

from app.models import Document, DocumentVersion
from app.services import storage


def delete_document(db: Session, document: Document) -> None:
    """Delete a document with every version, then its stored files.

    Args:
        db: Session to commit on.
        document: Document to remove.
    """
    course_id = document.course_id
    document_id = document.id
    db.delete(document)
    db.commit()

    shutil.rmtree(
        storage.abs_path(f"courses/{course_id}/documents/{document_id}"),
        ignore_errors=True,
    )


def delete_version(db: Session, document: Document, version: DocumentVersion) -> bool:
    """Remove one upload of a document, or the whole document if it is the last.

    Deleting a version takes its pages, extractions and embeddings with it, on
    the same cascade chain as a whole document, and removes only that version's
    files. Nothing needs re-embedding: every remaining version keeps its own
    pages and their vectors, so the index over the surviving uploads is already
    correct. Extractions elsewhere that were copied from these pages keep their
    Markdown, with source_page_id set to null.

    Version numbers of the survivors are deliberately not renumbered. The number
    identifies a specific upload, which reports, verdicts and the professor's
    memory all refer to as "v2", so closing the gap would silently rewrite
    history. A gap is the honest record: v1, v3 says v2 existed and was removed.

    Args:
        db: Session to commit on.
        document: Document owning the version.
        version: Version to remove.

    Returns:
        True when the whole document was deleted, so the caller can send the
        professor somewhere that still exists.
    """
    if len(document.versions) <= 1:
        delete_document(db, document)
        return True

    course_id = document.course_id
    document_id = document.id
    version_number = version.version_number
    db.delete(version)
    db.commit()

    shutil.rmtree(
        storage.abs_path(
            f"courses/{course_id}/documents/{document_id}/v{version_number}"
        ),
        ignore_errors=True,
    )
    return False
