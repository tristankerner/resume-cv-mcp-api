from datetime import datetime, timedelta
from typing import Any, ClassVar, cast

from sqlalchemy import JSON, CursorResult, ForeignKey, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .base import Clock, SQAlchemyBase


class PendingWrite(SQAlchemyBase):
    """One row per issued MCP write preview — the two-phase confirmation
    gate's whole state. See `services.confirmation.confirmation_service
    .ConfirmationService`, which is the only code that reads or writes this
    table.

    `payload` is the authority for what `confirm_*` writes, not whatever
    arguments a `confirm_*` call is made with — a `confirm_*` tool takes the
    token and nothing else. If a model could pass arguments alongside the
    token, it could preview something innocuous and commit something else,
    and the whole gate would be theatre. This is the one invariant a future
    edit to this table or its service is most likely to break: do not add a
    way for `redeem` to accept caller-supplied arguments that override
    `payload`.

    Modelled on `OAuthRefreshToken`: opaque token, hashed and never stored,
    single-use rather than revocable. `TTL` is far shorter than any
    credential's, because this token authorizes one specific write rather
    than a session — ten minutes is long enough for a human to read a
    preview and answer, short enough that a token left in a transcript is
    inert by the time anyone finds it.
    """

    __tablename__ = "pending_writes"

    TTL: ClassVar[timedelta] = timedelta(minutes=10)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(unique=True, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    # The tool that issued this preview. A token is not portable between
    # tools — redeeming it through any tool but the one that issued it is
    # refused, the same way a scope from one credential cannot be spent by
    # another.
    tool_name: Mapped[str] = mapped_column(nullable=False)
    # The exact arguments `confirm_*` will replay. See the class docstring.
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # What was shown to the user, kept for the audit trail — not read back by
    # `redeem`, only by whatever later needs to know what a token authorized.
    preview: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    consumed_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)

    def __repr__(self) -> str:
        return f"PendingWrite(id={self.id!r}, tool_name={self.tool_name!r})"

    def is_usable(self, now: datetime | None = None) -> bool:
        now = now or Clock.utcnow()
        return self.consumed_at is None and self.expires_at > now

    @staticmethod
    async def get_by_hash(db: AsyncSession, token_hash: str) -> PendingWrite | None:
        return (
            (
                await db.execute(
                    select(PendingWrite).where(PendingWrite.token_hash == token_hash)
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def lock_by_hash(db: AsyncSession, token_hash: str) -> PendingWrite | None:
        """Re-read with the row held, so two concurrent redemptions of the
        same token cannot both observe it as live. Same reasoning as
        `OAuthRefreshToken.lock_by_hash`."""
        return (
            (
                await db.execute(
                    select(PendingWrite)
                    .where(PendingWrite.token_hash == token_hash)
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def prune(db: AsyncSession) -> int:
        """Drop rows past their TTL, consumed or not.

        Unlike a refresh token, a consumed row carries no reuse-detection
        value — redeeming it twice is already refused by `consumed_at` alone
        — so there is no reason to keep it past expiry. Called from the same
        startup sweep as the other TTL'd tables; see main.py.
        """
        result = await db.execute(
            delete(PendingWrite).where(PendingWrite.expires_at < Clock.utcnow())
        )
        await db.commit()
        return cast(CursorResult, result).rowcount or 0
