from datetime import datetime

from sqlalchemy import JSON, ForeignKey, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column

from .base import Clock, SQAlchemyBase


class ApiKey(SQAlchemyBase):
    """A long-lived credential belonging to a user.

    Only the hash is stored. `prefix` is the non-secret half of the presented
    key and exists purely to make lookup a single indexed hit rather than a
    scan-and-compare over every row.
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(nullable=False)
    prefix: Mapped[str] = mapped_column(unique=True, index=True, nullable=False)
    key_hash: Mapped[str] = mapped_column(nullable=False)
    scopes: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)
    last_used_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]

    def __repr__(self) -> str:
        return f"ApiKey(id={self.id!r}, name={self.name!r}, prefix={self.prefix!r})"

    def is_usable(self, now: datetime | None = None) -> bool:
        now = now or Clock.utcnow()
        if self.revoked_at is not None:
            return False
        return not (self.expires_at is not None and self.expires_at <= now)

    @staticmethod
    async def get_by_prefix(db: AsyncSession, prefix: str) -> ApiKey | None:
        return (
            (await db.execute(select(ApiKey).where(ApiKey.prefix == prefix)))
            .scalars()
            .first()
        )

    @staticmethod
    async def get_by_id(db: AsyncSession, key_id: int) -> ApiKey | None:
        return (
            (await db.execute(select(ApiKey).where(ApiKey.id == key_id)))
            .scalars()
            .first()
        )

    @staticmethod
    async def list_for_user(db: AsyncSession, user_id: int) -> list[ApiKey]:
        result = await db.execute(
            select(ApiKey)
            .where(ApiKey.user_id == user_id)
            .order_by(ApiKey.created_at.desc())
        )
        return list(result.scalars().all())
