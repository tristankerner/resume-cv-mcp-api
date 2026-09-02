from datetime import datetime
from typing import Any, NamedTuple

from sqlalchemy import JSON, ForeignKey, Index, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column

from .base import Clock, SQAlchemyBase


class DocumentTypeConflict(Exception):
    """A write arrived for a document whose stored type does not match.

    A plain exception rather than an HTTPException: persistence stays free of
    the web framework, and the service layer decides what this means to a
    caller — see document_type_conflict in services/document/exceptions.
    """

    def __init__(self, existing_type: str, attempted_type: str):
        self.existing_type = existing_type
        self.attempted_type = attempted_type
        super().__init__(
            f"Document is type {existing_type!r}; cannot write it as "
            f"{attempted_type!r}."
        )


class DocumentNameConflict(Exception):
    """A rename target is already the name of one of this owner's documents.

    Plain exception, same reasoning as DocumentTypeConflict: persistence
    stays free of the web framework, and the service layer decides this means
    409 — see document_name_conflict in services/document/exceptions.
    """

    def __init__(self, name: str):
        self.name = name
        super().__init__(f"A document named {name!r} already exists.")


class Document(SQAlchemyBase):
    __tablename__ = "documents"
    __table_args__ = (Index("ix_documents_created_by_type", "created_by", "type"),)

    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id"), primary_key=True, nullable=False
    )
    name: Mapped[str] = mapped_column(primary_key=True, nullable=False)
    revision_id: Mapped[int] = mapped_column(primary_key=True, nullable=False)
    type: Mapped[str] = mapped_column(nullable=False)
    public: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)
    revision_note: Mapped[str | None]
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    def __repr__(self) -> str:
        return (
            f"Document(created_by={self.created_by!r}, name={self.name!r}, "
            f"revision_id={self.revision_id!r}, type={self.type!r})"
        )

    @staticmethod
    def block_mutations(session: Session, flush_context: Any, instances: Any) -> None:
        """Immutability of a *revision* is what this table guarantees, not
        permanence — deletion is a supported operation.

        Registered with `event.listen` at the foot of this module rather than
        with the `@event.listens_for` decorator, which would require a
        module-level function.
        """
        for obj in session.dirty:
            if isinstance(obj, Document) and session.is_modified(obj):
                raise PermissionError("Cannot update records in an append-only table.")

    @staticmethod
    async def get_latest(db: AsyncSession, owner_id: int, name: str) -> Document | None:
        stmt = (
            select(Document)
            .where(Document.created_by == owner_id, Document.name == name)
            .order_by(Document.revision_id.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_revisions(
        db: AsyncSession,
        owner_id: int,
        name: str,
        limit: int,
        newest_first: bool,
    ) -> list[Document]:
        """The `limit` most recent revisions, ordered as asked.

        `newest_first` governs only how this already-selected slice is
        arranged, not which revisions are selected: `limit=3,
        newest_first=False` is the three most recent revisions, oldest of
        those three first — not the three oldest revisions overall.
        """
        stmt = (
            select(Document)
            .where(Document.created_by == owner_id, Document.name == name)
            .order_by(Document.revision_id.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        revisions = list(result.scalars().all())
        if not newest_first:
            revisions.reverse()
        return revisions

    @staticmethod
    async def list_latest(db: AsyncSession, owner_id: int) -> list[Document]:
        """One row per document this owner holds: its highest revision_id."""
        latest_ids = (
            select(Document.name, func.max(Document.revision_id).label("revision_id"))
            .where(Document.created_by == owner_id)
            .group_by(Document.name)
            .subquery()
        )
        stmt = (
            select(Document)
            .join(
                latest_ids,
                (Document.name == latest_ids.c.name)
                & (Document.revision_id == latest_ids.c.revision_id),
            )
            .where(Document.created_by == owner_id)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def delete_all_revisions(db: AsyncSession, owner_id: int, name: str) -> int:
        stmt = select(Document).where(
            Document.created_by == owner_id, Document.name == name
        )
        result = await db.execute(stmt)
        revisions = list(result.scalars().all())
        for revision in revisions:
            await db.delete(revision)
        if revisions:
            await db.commit()
        return len(revisions)

    @staticmethod
    async def rename(
        db: AsyncSession, owner_id: int, old_name: str, new_name: str
    ) -> int:
        """Move every revision of a document to a new name, atomically.

        `documents` is append-only — see block_mutations below — so this
        cannot be an UPDATE of the `name` column. Instead every revision is
        cloned under the new name and the originals are deleted, in one
        transaction. The primary key is (created_by, name, revision_id), so
        the new rows cannot collide with the old ones and both can exist
        mid-transaction. Returns 0 for a name with no revisions to move,
        which the caller treats as "not found" — this method does not know
        whether that means the name never existed or belongs to someone else.
        """
        stmt = select(Document).where(
            Document.created_by == owner_id, Document.name == old_name
        )
        result = await db.execute(stmt)
        revisions = list(result.scalars().all())
        if not revisions:
            return 0

        if await Document.get_latest(db, owner_id, new_name) is not None:
            raise DocumentNameConflict(new_name)

        for revision in revisions:
            db.add(
                Document(
                    created_by=owner_id,
                    name=new_name,
                    revision_id=revision.revision_id,
                    type=revision.type,
                    public=revision.public,
                    created_at=revision.created_at,
                    revision_note=revision.revision_note,
                    data=revision.data,
                )
            )
        for revision in revisions:
            await db.delete(revision)
        await db.commit()
        return len(revisions)

    @staticmethod
    async def upsert_document(
        db: AsyncSession, document: Document, public: bool | None = None
    ) -> UpsertResult:
        """Write a new revision, unless nothing that defines one has changed.

        `public` is passed separately from the rest of `document` because it is
        the one field a write may decline to state: None means "whatever the
        current revision says", so an edit to `data` alone cannot flip the flag
        as a side effect. A document with no current revision defaults closed.
        """
        existing = await Document.get_latest(db, document.created_by, document.name)
        if existing is None:
            document.public = bool(public)
            document.revision_id = 1
        else:
            if existing.type != document.type:
                raise DocumentTypeConflict(existing.type, document.type)
            document.public = existing.public if public is None else public
            # A changed `revision_note` alone is still a no-op.
            if existing.data == document.data and existing.public == document.public:
                return UpsertResult(existing, created=False)
            document.revision_id = existing.revision_id + 1
        db.add(document)
        await db.commit()
        return UpsertResult(document, created=True)


class UpsertResult(NamedTuple):
    """What `upsert_document` did: the revision now current, and whether it
    had to write it.

    `created` is a plain bool rather than the enum the API answers with: that
    enum is the wire contract and lives with the response model, so naming it
    here would let a rename in persistence change the HTTP response. Same
    separation that keeps `Document.type` a plain string.

    `document` stays optional rather than the whole return being
    `UpsertResult | None`: `upsert_document` cannot produce `None` today, but
    `tests/test_defensive_paths.py` monkeypatches it to simulate the storage
    layer misbehaving.
    """

    document: Document | None
    created: bool


event.listen(Session, "before_flush", Document.block_mutations)
