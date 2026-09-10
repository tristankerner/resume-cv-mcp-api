from datetime import datetime, timedelta
from typing import ClassVar, cast

from sqlalchemy import CursorResult, ForeignKey, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .base import Clock, SQAlchemyBase


class AuthRefreshToken(SQAlchemyBase):
    """One link in a session's rotation chain, opaque and hashed like
    `OAuthRefreshToken` — read that model first; nearly every decision here
    repeats one already made there.

    Never a JWT — it must be revocable, and a self-verifying token is not.
    `session_id` is shared by every token descended from one login, so
    presenting a token whose `revoked_at` is already set — a reuse of a token
    this chain has moved past — revokes every other token in the chain by that
    one value, per RFC 6819's reuse-detection guidance.

    Distinct from `OAuthRefreshToken` because the risk profile is different: an
    interactive session has no `client_id` or `scopes` of its own (both are
    always the user's current role scopes), and its TTL is shorter than the
    OAuth default on purpose — see `TTL`.
    """

    __tablename__ = "auth_refresh_tokens"

    # 14 days by default — configurable via AUTH_REFRESH_TOKEN_EXPIRE_DAYS,
    # see ConfigServiceModel. Long enough that "keep me logged in" means
    # something, short enough to bound a stolen token. Shorter than
    # OAuthRefreshToken.TTL's 30 days: an interactive session is a different
    # risk profile and should not default to the same value. This constant is
    # the config default and a fallback for callers that build a row without a
    # settings object (none do today); `RefreshTokenIssuer` always computes the
    # real expiry from configuration.
    TTL: ClassVar[timedelta] = timedelta(days=14)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(unique=True, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[datetime | None]
    rotated_to_id: Mapped[int | None] = mapped_column(
        ForeignKey("auth_refresh_tokens.id")
    )
    user_agent: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)
    last_used_at: Mapped[datetime | None]

    def __repr__(self) -> str:
        return f"AuthRefreshToken(id={self.id!r}, session_id={self.session_id!r})"

    def is_usable(self, now: datetime | None = None) -> bool:
        now = now or Clock.utcnow()
        return self.revoked_at is None and self.expires_at > now

    @staticmethod
    async def get_by_hash(db: AsyncSession, token_hash: str) -> AuthRefreshToken | None:
        return (
            (
                await db.execute(
                    select(AuthRefreshToken).where(
                        AuthRefreshToken.token_hash == token_hash
                    )
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def lock_by_hash(
        db: AsyncSession, token_hash: str
    ) -> AuthRefreshToken | None:
        """Re-read with the row held, so two concurrent uses of the same
        refresh token cannot both observe it as live. Same reasoning as
        `OAuthRefreshToken.lock_by_hash`."""
        return (
            (
                await db.execute(
                    select(AuthRefreshToken)
                    .where(AuthRefreshToken.token_hash == token_hash)
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def revoke_chain(db: AsyncSession, session_id: str) -> None:
        """Revoke every live token descended from one session, on reuse of any
        of them. Selected and updated in Python rather than a bulk UPDATE, so
        the ORM's identity map stays consistent with any row from this chain
        already loaded in the session."""
        now = Clock.utcnow()
        rows = (
            (
                await db.execute(
                    select(AuthRefreshToken).where(
                        AuthRefreshToken.session_id == session_id,
                        AuthRefreshToken.revoked_at.is_(None),
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
        the reuse signal `revoke_chain` exists to act on. Called from the same
        startup sweep as `OAuthRefreshToken.prune` — see main.py.
        """
        result = await db.execute(
            delete(AuthRefreshToken).where(AuthRefreshToken.expires_at < Clock.utcnow())
        )
        await db.commit()
        return cast(CursorResult, result).rowcount or 0
