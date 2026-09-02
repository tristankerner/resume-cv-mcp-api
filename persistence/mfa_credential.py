from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import CursorResult, ForeignKey, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.attributes import set_committed_value

from .base import Clock, SQAlchemyBase
from .mfa_backup_code import MfaBackupCode


class MfaCredential(SQAlchemyBase):
    """One second factor belonging to a user.

    `kind` names which MfaMethod owns the row — see
    services/auth/mfa/registry.py. Everything method-specific hangs off that:
    a TOTP row uses `secret` and `last_used_step`, a backup-code row uses
    neither and owns a set of MfaBackupCode children instead. Adding SMS
    later adds a kind and a method class, not a column here, unless that
    method genuinely needs storage of its own.

    `activated_at` is NULL between "the secret was issued" and "the user
    proved they can produce a code from it". A NULL row satisfies nothing and
    is invisible to the login path — enrolling and then closing the tab must
    not lock anyone out.
    """

    __tablename__ = "mfa_credentials"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(nullable=False)
    label: Mapped[str] = mapped_column(nullable=False)
    # Sealed, not plaintext — MfaSecretBox owns the format and the "v1:" prefix
    # that marks it. Nothing outside TotpMethod reads this column. Unbounded
    # String, so a Fernet token needs no migration for length.
    secret: Mapped[str | None]
    # The TOTP time step most recently accepted for this credential. A code is
    # refused if its step is not strictly greater, which is what stops the
    # same six digits being replayed inside their thirty-second window.
    last_used_step: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)
    activated_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]

    def __repr__(self) -> str:
        return (
            f"MfaCredential(id={self.id!r}, kind={self.kind!r}, label={self.label!r})"
        )

    @property
    def is_active(self) -> bool:
        return self.activated_at is not None

    @staticmethod
    async def list_for_user(db: AsyncSession, user_id: int) -> list[MfaCredential]:
        result = await db.execute(
            select(MfaCredential)
            .where(MfaCredential.user_id == user_id)
            .order_by(MfaCredential.created_at)
        )
        return list(result.scalars().all())

    @staticmethod
    async def list_active_for_user(
        db: AsyncSession, user_id: int
    ) -> list[MfaCredential]:
        result = await db.execute(
            select(MfaCredential)
            .where(
                MfaCredential.user_id == user_id,
                MfaCredential.activated_at.is_not(None),
            )
            .order_by(MfaCredential.created_at)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_for_user(
        db: AsyncSession, user_id: int, credential_id: int
    ) -> MfaCredential | None:
        """Scoped to the owner deliberately: a caller may only name their own
        credential, so an id belonging to somebody else is simply not found."""
        return (
            (
                await db.execute(
                    select(MfaCredential).where(
                        MfaCredential.id == credential_id,
                        MfaCredential.user_id == user_id,
                    )
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def claim_totp_step(
        credential: MfaCredential,
        db: AsyncSession,
        step: int,
        now: datetime,
        secret: str,
    ) -> bool:
        """Record `step` as used, if and only if nothing at or past it has
        been recorded already. Returns whether this caller won the claim.

        One statement rather than a read-modify-write: two requests presenting
        the same code both read `last_used_step` before either writes, so
        comparing in Python would accept the code twice — the real-time relay
        this guard exists to stop. `with_for_update()` would close that on
        Postgres and silently do nothing on SQLite (see `User.lock_for_update`);
        a conditional UPDATE is atomic on both.

        `secret` rides along because the lazy re-seal writes the same row.
        """
        result = await db.execute(
            update(MfaCredential)
            .where(
                MfaCredential.id == credential.id,
                or_(
                    MfaCredential.last_used_step.is_(None),
                    MfaCredential.last_used_step < step,
                ),
            )
            .values(last_used_step=step, last_used_at=now, secret=secret)
            .execution_options(synchronize_session=False)
        )
        if not cast(CursorResult, result).rowcount:
            return False

        # The UPDATE went round the ORM. Marked as already-persisted rather
        # than assigned, so the instance matches the row without the next flush
        # re-writing it.
        set_committed_value(credential, "last_used_step", step)
        set_committed_value(credential, "last_used_at", now)
        set_committed_value(credential, "secret", secret)
        return True

    @staticmethod
    async def delete_all_for_user(db: AsyncSession, user_id: int) -> int:
        """Every credential, activated or not. Returns how many rows went.

        Children go first: SQLite does not enforce foreign keys by default,
        so relying on a cascade would leave orphaned backup codes there and
        not on Postgres — a difference the test suite would never see.

        Does not commit — the caller owns the transaction.
        """
        ids = (
            (
                await db.execute(
                    select(MfaCredential.id).where(MfaCredential.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        if not ids:
            return 0
        await db.execute(
            delete(MfaBackupCode).where(MfaBackupCode.credential_id.in_(ids))
        )
        result = await db.execute(
            delete(MfaCredential).where(MfaCredential.user_id == user_id)
        )
        return cast(CursorResult, result).rowcount or 0
