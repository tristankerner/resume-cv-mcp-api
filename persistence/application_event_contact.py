from collections.abc import Collection
from datetime import datetime
from typing import ClassVar

from sqlalchemy import ForeignKey, Index, UniqueConstraint, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .base import Clock, SQAlchemyBase


class ApplicationEventContact(SQAlchemyBase):
    """Which contacts an application event names. Replaces
    `ApplicationEvent.contact_id`, which held at most one.

    Deliberately minimal — no role, no per-contact note. An event already
    carries a `description`; a second free-text field beside it would only
    create a question about which one a given sentence belongs in.
    """

    __tablename__ = "application_event_contacts"
    __table_args__ = (
        UniqueConstraint(
            "event_id", "contact_id", name="uq_application_event_contacts_pair"
        ),
        Index("ix_application_event_contacts_user_event", "user_id", "event_id"),
        Index("ix_application_event_contacts_user_contact", "user_id", "contact_id"),
    )

    MAX_PER_EVENT: ClassVar[int] = 25

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("application_events.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)

    def __repr__(self) -> str:
        return (
            f"ApplicationEventContact(id={self.id!r}, event_id={self.event_id!r}, "
            f"contact_id={self.contact_id!r})"
        )

    @staticmethod
    async def contact_ids_for(
        db: AsyncSession, user_id: int, event_ids: Collection[int]
    ) -> dict[int, list[int]]:
        """`{event_id: [contact_id, ...]}` for a whole page of events, in one
        query. Ordered `(event_id, id)` - insertion order, per §3.1's rule."""
        if not event_ids:
            return {}
        rows = (
            await db.execute(
                select(
                    ApplicationEventContact.event_id, ApplicationEventContact.contact_id
                )
                .where(
                    ApplicationEventContact.user_id == user_id,
                    ApplicationEventContact.event_id.in_(event_ids),
                )
                .order_by(ApplicationEventContact.event_id, ApplicationEventContact.id)
            )
        ).all()
        result: dict[int, list[int]] = {}
        for event_id, contact_id in rows:
            result.setdefault(event_id, []).append(contact_id)
        return result

    @staticmethod
    async def replace_for_event(
        db: AsyncSession, user_id: int, event_id: int, contact_ids: list[int]
    ) -> None:
        """Delete every row for `(user_id, event_id)`, then insert one row
        per id in the order given. Replacement, not diffing: the sets are at
        most `MAX_PER_EVENT` rows and a diff would be more code for no
        measurable gain. Does not commit - the caller owns the transaction."""
        await db.execute(
            delete(ApplicationEventContact).where(
                ApplicationEventContact.user_id == user_id,
                ApplicationEventContact.event_id == event_id,
            )
        )
        for contact_id in contact_ids:
            db.add(
                ApplicationEventContact(
                    user_id=user_id, event_id=event_id, contact_id=contact_id
                )
            )

    @staticmethod
    async def delete_for_events(
        db: AsyncSession, user_id: int, event_ids: Collection[int]
    ) -> None:
        """Bulk delete for the events an application is deleting. No-op on
        an empty list. Does not commit - the caller owns the transaction."""
        if not event_ids:
            return
        await db.execute(
            delete(ApplicationEventContact).where(
                ApplicationEventContact.user_id == user_id,
                ApplicationEventContact.event_id.in_(event_ids),
            )
        )

    @staticmethod
    async def delete_for_contact(
        db: AsyncSession, user_id: int, contact_id: int
    ) -> None:
        """Every row naming that contact, for `ContactService.delete_contact`.
        Does not commit - the caller owns the transaction."""
        await db.execute(
            delete(ApplicationEventContact).where(
                ApplicationEventContact.user_id == user_id,
                ApplicationEventContact.contact_id == contact_id,
            )
        )
