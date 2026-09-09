"""Course deletion.

Removing a course wipes everything belonging to it: documents (with their
versions, pages, extractions and stored files), rules, report runs, the written
reports produced by those runs, and the page embeddings used for retrieval.

All of it is one DELETE. The course is the aggregate root, and every foreign
key pointing at it, directly (documents, rules, report runs, embeddings) or
transitively, is declared ON DELETE CASCADE, so the database removes the
subtree in a single statement. Nothing is deleted explicitly here: an ordering
kept by hand is exactly what let delete_document miss embeddings and fail on a
constraint violation.
"""

import shutil

from sqlalchemy.orm import Session

from app.models import Course
from app.services import storage


def delete_course(db: Session, course: Course) -> None:
    """Delete a course, then its stored files.

    The files go last: a failed rmtree leaves orphaned bytes on disk, while a
    failed DELETE after the files are gone would leave rows pointing nowhere.

    Args:
        db: Session to commit on.
        course: Course to remove.
    """
    course_id = course.id

    db.delete(course)
    db.commit()

    shutil.rmtree(
        storage.abs_path(f"courses/{course_id}"),
        ignore_errors=True,
    )
