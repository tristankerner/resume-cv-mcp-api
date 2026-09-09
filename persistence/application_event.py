from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .base import Clock, SQAlchemyBase
from .batch_count import BatchCount


class ApplicationEvent(SQAlchemyBase):
    """One thing that happened on an application: a status change, a note, a
    rating, or any combination. `status` is nullable so a bare note can be
    recorded without pretending a status changed - see
    `services/tracking/application_service.py`'s `_recompute_status`.
    """

    __tablename__ = "application_events"
    __table_args__ = (
        CheckConstraint(
            "rating IS NULL OR (rating BETWEEN 1 AND 10)",
            name="ck_application_events_rating_range",
        ),
        Index(
            "ix_application_events_user_app_occurred",
            "user_id",
            "application_id",
            "occurred_at",
        ),
        Index("ix_application_events_user_occurred", "user_id", "occurred_at"),
        Index("ix_application_events_user_contact", "user_id", "contact_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str | None]
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    description: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[int | None]
    occurred_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)

    def __repr__(self) -> str:
        return (
            f"ApplicationEvent(id={self.id!r}, "
            f"application_id={self.application_id!r}, status={self.status!r})"
        )

    @staticmethod
    async def get(
        db: AsyncSession, user_id: int, event_id: int
    ) -> ApplicationEvent | None:
        return (
            (
                await db.execute(
                    select(ApplicationEvent).where(
                        ApplicationEvent.id == event_id,
                        ApplicationEvent.user_id == user_id,
                    )
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def counts_for(
        db: AsyncSession, user_id: int, application_ids: list[int]
    ) -> dict[int, int]:
        """Event count per application id, in one grouped query.

        The list endpoints need a count per row; loading each application's
        events to call `len` on them is a query per row, and 200 of those is
        what `limit` allows.
        """
        return await BatchCount.for_keys(
            db,
            key_column=ApplicationEvent.application_id,
            owner_column=ApplicationEvent.user_id,
            owner_id=user_id,
            keys=application_ids,
        )

    @staticmethod
    async def list_for_application(
        db: AsyncSession, user_id: int, application_id: int
    ) -> list[ApplicationEvent]:
        return list(
            (
                await db.execute(
                    select(ApplicationEvent)
                    .where(
                        ApplicationEvent.user_id == user_id,
                        ApplicationEvent.application_id == application_id,
                    )
                    .order_by(
                        ApplicationEvent.occurred_at.desc(), ApplicationEvent.id.desc()
                    )
                )
            )
            .scalars()
            .all()
        )

    @staticmethod
    async def latest_with_status(
        db: AsyncSession, user_id: int, application_id: int
    ) -> ApplicationEvent | None:
        return (
            (
                await db.execute(
                    select(ApplicationEvent)
                    .where(
                        ApplicationEvent.user_id == user_id,
                        ApplicationEvent.application_id == application_id,
                        ApplicationEvent.status.is_not(None),
                    )
                    .order_by(
                        ApplicationEvent.occurred_at.desc(), ApplicationEvent.id.desc()
                    )
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
