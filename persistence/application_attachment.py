from datetime import datetime

from sqlalchemy import ForeignKey, Index, Text, UniqueConstraint, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, defer, mapped_column

from .base import Clock, SQAlchemyBase


class ApplicationAttachment(SQAlchemyBase):
    """A file attached to an application, stored as base64 in `content_base64`.

    Every query that only needs metadata must `defer` that column - see
    `list_metadata_for_application` and `get_metadata` below. Never load the
    blob to count or list.
    """

    __tablename__ = "application_attachments"
    __table_args__ = (
        Index("ix_application_attachments_user_app", "user_id", "application_id"),
        UniqueConstraint(
            "user_id",
            "application_id",
            "sha256",
            name="uq_application_attachments_sha",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(nullable=False)
    filename: Mapped[str] = mapped_column(nullable=False)
    content_type: Mapped[str] = mapped_column(nullable=False)
    byte_size: Mapped[int] = mapped_column(nullable=False)
    sha256: Mapped[str] = mapped_column(nullable=False)
    content_base64: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)

    def __repr__(self) -> str:
        return (
            f"ApplicationAttachment(id={self.id!r}, "
            f"application_id={self.application_id!r}, filename={self.filename!r})"
        )

    @staticmethod
    async def counts_for(
        db: AsyncSession, user_id: int, application_ids: list[int]
    ) -> dict[int, int]:
        """Attachment count per application id, in one grouped query - see
        `ApplicationEvent.counts_for` for why this is not a count per row."""
        if not application_ids:
            return {}
        rows = (
            await db.execute(
                select(ApplicationAttachment.application_id, func.count())
                .where(
                    ApplicationAttachment.user_id == user_id,
                    ApplicationAttachment.application_id.in_(application_ids),
                )
                .group_by(ApplicationAttachment.application_id)
            )
        ).all()
        counts = {row[0]: row[1] for row in rows}
        return {
            application_id: counts.get(application_id, 0)
            for application_id in application_ids
        }

    @staticmethod
    async def list_metadata_for_application(
        db: AsyncSession, user_id: int, application_id: int
    ) -> list[ApplicationAttachment]:
        return list(
            (
                await db.execute(
                    select(ApplicationAttachment)
                    .options(defer(ApplicationAttachment.content_base64))
                    .where(
                        ApplicationAttachment.user_id == user_id,
                        ApplicationAttachment.application_id == application_id,
                    )
                    .order_by(ApplicationAttachment.created_at.desc())
                )
            )
            .scalars()
            .all()
        )

    @staticmethod
    async def get_metadata(
        db: AsyncSession, user_id: int, attachment_id: int
    ) -> ApplicationAttachment | None:
        return (
            (
                await db.execute(
                    select(ApplicationAttachment)
                    .options(defer(ApplicationAttachment.content_base64))
                    .where(
                        ApplicationAttachment.id == attachment_id,
                        ApplicationAttachment.user_id == user_id,
                    )
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def get_with_content(
        db: AsyncSession, user_id: int, attachment_id: int
    ) -> ApplicationAttachment | None:
        return (
            (
                await db.execute(
                    select(ApplicationAttachment).where(
                        ApplicationAttachment.id == attachment_id,
                        ApplicationAttachment.user_id == user_id,
                    )
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def get_by_digest(
        db: AsyncSession, user_id: int, application_id: int, sha256: str
    ) -> ApplicationAttachment | None:
        return (
            (
                await db.execute(
                    select(ApplicationAttachment)
                    .options(defer(ApplicationAttachment.content_base64))
                    .where(
                        ApplicationAttachment.user_id == user_id,
                        ApplicationAttachment.application_id == application_id,
                        ApplicationAttachment.sha256 == sha256,
                    )
                )
            )
            .scalars()
            .first()
        )
