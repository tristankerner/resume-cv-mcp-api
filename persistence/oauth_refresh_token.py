from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import JSON, CursorResult, ForeignKey, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column

from .base import SQAlchemyBase, utcnow

# 30 days.
TTL = timedelta(days=30)


class OAuthRefreshToken(SQAlchemyBase):
    """One link in a rotation chain, opaque and hashed like an API key.

    Never a JWT — it must be revocable, which a self-verifying token is not.
    `grant_id` is shared by every token descended from one authorization
    grant, so presenting a token whose `revoked_at` is already set — a reuse
    of a token this chain has moved past — can revoke every other token in
    the chain by that one value, per RFC 6819's reuse-detection guidance.
    """

    __tablename__ = "oauth_refresh_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(unique=True, index=True, nullable=False)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    scopes: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), nullable=False
    )
    resource: Mapped[str | None]
    grant_id: Mapped[str] = mapped_column(nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[datetime | None]
    rotated_to_id: Mapped[int | None] = mapped_column(
        ForeignKey("oauth_refresh_tokens.id")
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    def __repr__(self) -> str:
        return f"OAuthRefreshToken(id={self.id!r}, grant_id={self.grant_id!r})"

    def is_usable(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return self.revoked_at is None and self.expires_at > now

    @staticmethod
    async def get_by_hash(
        db: AsyncSession, token_hash: str
    ) -> OAuthRefreshToken | None:
        return (
            (
                await db.execute(
                    select(OAuthRefreshToken).where(
                        OAuthRefreshToken.token_hash == token_hash
                    )
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def lock_by_hash(
        db: AsyncSession, token_hash: str
    ) -> OAuthRefreshToken | None:
        """Re-read with the row held, so two concurrent uses of the same
        refresh token cannot both observe it as live. Same reasoning as
        User.lock_for_update."""
        return (
            (
                await db.execute(
                    select(OAuthRefreshToken)
                    .where(OAuthRefreshToken.token_hash == token_hash)
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def revoke_chain(db: AsyncSession, grant_id: str) -> None:
        """Revoke every live token descended from one grant, on reuse of any
        of them. Selected and updated in Python rather than a bulk UPDATE, so
        the ORM's identity map stays consistent with any row from this chain
        already loaded in the session."""
        now = utcnow()
        rows = (
            (
                await db.execute(
                    select(OAuthRefreshToken).where(
                        OAuthRefreshToken.grant_id == grant_id,
                        OAuthRefreshToken.revoked_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.revoked_at = now
        await db.commit()

    @staticmethod
    async def prune(db: AsyncSession) -> int:
        """Drop tokens past their TTL, revoked or not.

        A revoked-but-unexpired row is kept: presenting it again is exactly
        the reuse signal `revoke_chain` exists to act on. Called from the
        same startup sweep as `AuthFailure.prune` — see main.py.
        """
        result = await db.execute(
            delete(OAuthRefreshToken).where(OAuthRefreshToken.expires_at < utcnow())
        )
        await db.commit()
        return cast(CursorResult, result).rowcount or 0

    @staticmethod
    async def delete_for_client(db: AsyncSession, client_id: str) -> int:
        """Every token descended from this client, live, revoked or expired.
        Does not commit — called ahead of deleting the client row itself, in
        one transaction; see OAuthClientAdminService.delete_client."""
        result = await db.execute(
            delete(OAuthRefreshToken).where(OAuthRefreshToken.client_id == client_id)
        )
        return cast(CursorResult, result).rowcount or 0
