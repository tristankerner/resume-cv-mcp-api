from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import JSON, CursorResult, ForeignKey, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.attributes import set_committed_value

from .base import Clock, SQAlchemyBase


class PasskeyCredential(SQAlchemyBase):
    """One WebAuthn credential belonging to a user.

    Not an `mfa_credentials` row: a passkey is a first factor here, not a
    second one — see services/auth/passkeys/login.py. There is also no
    `activated_at`. A TOTP secret needs a proof-of-possession round trip
    before it can be trusted; a registration response *is* that proof, so a
    row exists only once it is usable.
    """

    __tablename__ = "passkey_credentials"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    # base64url of the raw credential id. Unique across every account, not just
    # within one: the login path looks a credential up by this alone, before it
    # knows whose it is, and two accounts sharing one would make that lookup
    # ambiguous at exactly the wrong moment.
    credential_id: Mapped[str] = mapped_column(unique=True, nullable=False)
    # base64url of the COSE-encoded public key. Public by definition; unlike a
    # TOTP seed there is nothing here for MfaSecretBox to seal.
    public_key: Mapped[str] = mapped_column(nullable=False)
    # The authenticator's own monotonic counter. Many passkey authenticators
    # report a constant 0 and never increment — see `claim_sign_count`.
    sign_count: Mapped[int] = mapped_column(default=0, nullable=False)
    # e.g. ["internal", "hybrid"]. Echoed back in `allowCredentials` so a
    # browser can say "use your phone" rather than "insert a security key".
    transports: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), default=list, nullable=False
    )
    aaguid: Mapped[str | None]
    # "single_device" or "multi_device", from the BE/BS flags.
    device_type: Mapped[str] = mapped_column(nullable=False)
    backed_up: Mapped[bool] = mapped_column(default=False, nullable=False)
    label: Mapped[str] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)
    last_used_at: Mapped[datetime | None]

    def __repr__(self) -> str:
        return f"PasskeyCredential(id={self.id!r}, label={self.label!r})"

    @staticmethod
    async def list_for_user(db: AsyncSession, user_id: int) -> list[PasskeyCredential]:
        result = await db.execute(
            select(PasskeyCredential)
            .where(PasskeyCredential.user_id == user_id)
            .order_by(PasskeyCredential.created_at)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_for_user(
        db: AsyncSession, user_id: int, credential_id: int
    ) -> PasskeyCredential | None:
        """Scoped to the owner deliberately: a caller may only name their own
        credential, so an id belonging to somebody else is simply not found."""
        return (
            (
                await db.execute(
                    select(PasskeyCredential).where(
                        PasskeyCredential.id == credential_id,
                        PasskeyCredential.user_id == user_id,
                    )
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def get_by_credential_id(
        db: AsyncSession, credential_id: str
    ) -> PasskeyCredential | None:
        """The login-path lookup: unscoped, since the caller does not know
        whose credential this is until this returns."""
        return (
            (
                await db.execute(
                    select(PasskeyCredential).where(
                        PasskeyCredential.credential_id == credential_id
                    )
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def delete_all_for_user(db: AsyncSession, user_id: int) -> int:
        """Every passkey the account holds. Returns how many rows went.

        No child table, so no cascade problem — unlike
        `MfaCredential.delete_all_for_user`. Does not commit; the caller owns
        the transaction.
        """
        result = await db.execute(
            delete(PasskeyCredential).where(PasskeyCredential.user_id == user_id)
        )
        return cast(CursorResult, result).rowcount or 0

    @staticmethod
    async def claim_sign_count(
        credential: PasskeyCredential,
        db: AsyncSession,
        new_count: int,
        now: datetime,
    ) -> bool:
        """Record `new_count`, if and only if it is an advance. Returns whether
        this caller won the claim.

        A conditional UPDATE for the same reason `MfaCredential.claim_totp_step`
        is one: py_webauthn already refuses a counter that went backwards, but
        two requests replaying the same assertion both read the stored count
        before either writes, so the library check passes twice and only the
        database can break the tie.

        `new_count == 0` is not a replay. Most platform authenticators — every
        synced passkey among them — report a constant zero and offer no replay
        signal at all; the challenge's own five-minute lifetime is what bounds
        that case, and refusing zero would refuse every Apple and Google
        passkey there is.
        """
        if new_count == 0:
            credential.last_used_at = now
            return True

        result = await db.execute(
            update(PasskeyCredential)
            .where(
                PasskeyCredential.id == credential.id,
                PasskeyCredential.sign_count < new_count,
            )
            .values(sign_count=new_count, last_used_at=now)
            .execution_options(synchronize_session=False)
        )
        if not cast(CursorResult, result).rowcount:
            return False

        # The UPDATE went round the ORM. Marked as already-persisted rather
        # than assigned, so the instance matches the row without the next
        # flush re-writing it.
        set_committed_value(credential, "sign_count", new_count)
        set_committed_value(credential, "last_used_at", now)
        return True
