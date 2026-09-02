from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import CursorResult, ForeignKey, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.attributes import set_committed_value

from .base import Clock, SQAlchemyBase


class MfaBackupCode(SQAlchemyBase):
    """One single-use recovery code, belonging to a backup-code credential.

    A child table rather than a JSON list on the credential: each code needs
    its own `used_at`, "how many are left" should be a count rather than a
    parse, and marking one used must not rewrite the whole set.

    Only the SHA-256 hash is stored, for the same reason API keys are hashed
    that way rather than with argon2 — these are generated secrets with no
    dictionary to attack, and the verification path must not be slow.
    """

    __tablename__ = "mfa_backup_codes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    credential_id: Mapped[int] = mapped_column(
        ForeignKey("mfa_credentials.id"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)
    used_at: Mapped[datetime | None]

    def __repr__(self) -> str:
        return f"MfaBackupCode(id={self.id!r}, credential_id={self.credential_id!r})"

    @staticmethod
    async def list_unused(db: AsyncSession, credential_id: int) -> list[MfaBackupCode]:
        result = await db.execute(
            select(MfaBackupCode).where(
                MfaBackupCode.credential_id == credential_id,
                MfaBackupCode.used_at.is_(None),
            )
        )
        return list(result.scalars().all())

    @staticmethod
    async def count_unused(db: AsyncSession, credential_id: int) -> int:
        result = await db.execute(
            select(func.count(MfaBackupCode.id)).where(
                MfaBackupCode.credential_id == credential_id,
                MfaBackupCode.used_at.is_(None),
            )
        )
        return result.scalar_one()

    @staticmethod
    async def claim(code: MfaBackupCode, db: AsyncSession, now: datetime) -> bool:
        """Spend one code, if and only if it is still unused. Returns whether
        this caller won it.

        Single-use has to survive two requests presenting the same code at
        once, which a read-then-assign does not — see
        `MfaCredential.claim_totp_step` for why this is a conditional UPDATE
        and not a row lock.
        """
        result = await db.execute(
            update(MfaBackupCode)
            .where(MfaBackupCode.id == code.id, MfaBackupCode.used_at.is_(None))
            .values(used_at=now)
            .execution_options(synchronize_session=False)
        )
        if not cast(CursorResult, result).rowcount:
            return False
        set_committed_value(code, "used_at", now)
        return True

    @staticmethod
    async def delete_for_credential(db: AsyncSession, credential_id: int) -> None:
        """Used when a set is regenerated. Does not commit."""
        await db.execute(
            delete(MfaBackupCode).where(MfaBackupCode.credential_id == credential_id)
        )
