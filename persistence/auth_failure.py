from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import CursorResult, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .base import SQAlchemyBase, utcnow


class AuthFailure(SQAlchemyBase):
    """Failed password logins, tallied per calling address.

    One row per address rather than one per attempt. An event log would carry
    better forensics, but the events it records are exactly the traffic an
    attacker controls, so the table grows as fast as they can send requests;
    an aggregate is bounded by the number of distinct addresses instead, and
    keeps the count and the first and last sighting, which is most of what the
    log was for.

    Rows exist only while they say something. `prune` drops the ones whose
    window has lapsed and whose ban has expired, because such a row is
    indistinguishable from no row at all.
    """

    __tablename__ = "auth_failures"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(unique=True, index=True, nullable=False)
    failure_count: Mapped[int] = mapped_column(default=0, nullable=False)
    first_failure_at: Mapped[datetime | None]
    last_failure_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    banned_until: Mapped[datetime | None]

    def __init__(self, **kwargs):
        """Counter defaults at construction. Same reasoning as User.__init__."""
        kwargs.setdefault("failure_count", 0)
        kwargs.setdefault("last_failure_at", utcnow())
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        return (
            f"AuthFailure(address={self.address!r}, "
            f"failure_count={self.failure_count!r})"
        )

    @staticmethod
    async def get_by_address(db: AsyncSession, address: str) -> AuthFailure | None:
        return (
            (
                await db.execute(
                    select(AuthFailure).where(AuthFailure.address == address)
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def lock_for_update(db: AsyncSession, address: str) -> AuthFailure | None:
        """Re-read with the row held. Same reasoning as User.lock_for_update."""
        return (
            (
                await db.execute(
                    select(AuthFailure)
                    .where(AuthFailure.address == address)
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def prune(db: AsyncSession, window: timedelta) -> int:
        """Drop rows that no longer influence any decision.

        Called at startup rather than on a schedule: there is no scheduler
        here, and a scale-to-zero deployment starts often enough that the
        table cannot grow far between sweeps. Returns the number removed.
        """
        cutoff = utcnow() - window
        result = await db.execute(
            delete(AuthFailure).where(
                AuthFailure.last_failure_at < cutoff,
                or_(
                    AuthFailure.banned_until.is_(None),
                    AuthFailure.banned_until < utcnow(),
                ),
            )
        )
        await db.commit()
        # execute() is annotated as returning Result, but a DELETE always
        # yields a CursorResult, which is what carries rowcount.
        return cast(CursorResult, result).rowcount or 0
