"""Course model: the unit of work everything else hangs from."""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Course(Base):
    """A course owned by one professor, holding its documents and rules."""

    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    owner = relationship("User")
    # passive_deletes lets the database's ON DELETE CASCADE do the work: the ORM
    # neither loads the children nor issues a DELETE per row. Every relationship
    # mirroring a cascading foreign key is declared this way, so deletion
    # behaviour lives in one place, the schema.
    documents = relationship(
        "Document",
        back_populates="course",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Document.created_at.desc()",
    )
    rules = relationship(
        "CourseRule",
        back_populates="course",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="CourseRule.created_at",
    )
