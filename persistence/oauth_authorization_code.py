from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import JSON, CursorResult, ForeignKey, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column

from .base import SQAlchemyBase, utcnow

# 60 seconds — long enough for the redirect round trip,
# short enough that a code leaked in a referrer or a log is worthless by the
# time anyone could use it.
TTL = timedelta(seconds=60)


class OAuthAuthorizationCode(SQAlchemyBase):
    """A single-use grant from `/oauth/authorize` to `/oauth/token`.

    Hashed at rest like a refresh token or an API key — the code is a bearer
    secret for the sixty seconds it lives. `redirect_uri` and
    `code_challenge` are copied from the authorize request so the token
    endpoint can bind the exchange to them without a second lookup.
    """

    __tablename__ = "oauth_authorization_codes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code_hash: Mapped[str] = mapped_column(unique=True, index=True, nullable=False)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    redirect_uri: Mapped[str] = mapped_column(nullable=False)
    scopes: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), nullable=False
    )
    code_challenge: Mapped[str] = mapped_column(nullable=False)
    code_challenge_method: Mapped[str] = mapped_column(nullable=False)
    resource: Mapped[str | None]
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    consumed_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    def __repr__(self) -> str:
        return f"OAuthAuthorizationCode(id={self.id!r}, client_id={self.client_id!r})"

    def is_usable(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return self.consumed_at is None and self.expires_at > now

    @staticmethod
    async def get_by_hash(
        db: AsyncSession, code_hash: str
    ) -> OAuthAuthorizationCode | None:
        return (
            (
                await db.execute(
                    select(OAuthAuthorizationCode).where(
                        OAuthAuthorizationCode.code_hash == code_hash
                    )
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def lock_by_hash(
        db: AsyncSession, code_hash: str
    ) -> OAuthAuthorizationCode | None:
        """Re-read with the row held, so two concurrent redemptions of the
        same code cannot both observe it as unconsumed. Same reasoning as
        User.lock_for_update."""
        return (
            (
                await db.execute(
                    select(OAuthAuthorizationCode)
                    .where(OAuthAuthorizationCode.code_hash == code_hash)
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def prune(db: AsyncSession) -> int:
        """Drop codes that can no longer be exchanged, consumed or not.

        Called from the same startup sweep as `AuthFailure.prune` — see
        main.py — for the same reason: no scheduler, and frequent cold
        starts keep the table from growing far between sweeps.
        """
        result = await db.execute(
            delete(OAuthAuthorizationCode).where(
                OAuthAuthorizationCode.expires_at < utcnow()
            )
        )
        await db.commit()
        return cast(CursorResult, result).rowcount or 0
