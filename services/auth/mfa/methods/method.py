from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.mfa_credential import MfaCredential
from persistence.user import User
from services.auth.exceptions import MfaErrors
from services.auth.mfa.kinds import MfaMethodKind
from services.config.config_service import ConfigServiceModel


class EnrollmentResult(BaseModel):
    """What enrolment hands back to the caller, once.

    `secret` and `codes` are the only time these values exist outside the
    user's own device — nothing stores them recoverably, and neither is ever
    returned by a later read.
    """

    credential_id: int
    kind: MfaMethodKind
    label: str
    secret: str | None = None  # TOTP: the base32 seed
    otpauth_uri: str | None = None  # TOTP: what a QR encodes
    codes: list[str] | None = None  # backup codes: the plaintext set


class MfaMethod(ABC):
    """One kind of second factor.

    Bound to a database session and constructed by MfaMethodRegistry, never
    directly by a router or a service. Adding SMS means adding a subclass and
    one registry entry; nothing outside this package learns a new name.

    Every method mutates rows and never commits. The caller owns the
    transaction, matching AccountPolicy.register_failure and every other
    policy object in services/auth.
    """

    KIND: ClassVar[MfaMethodKind]
    LABEL: ClassVar[str]
    # Whether a user may hold more than one credential of this kind. True for
    # TOTP (a phone and a laptop are two authenticators); False for backup
    # codes, where a second set would only be an unbounded pile of live
    # secrets — regenerating replaces.
    ALLOWS_MULTIPLE: ClassVar[bool]
    # Whether enrolment is two-phase. TOTP is: the secret is worthless until
    # the user proves their authenticator produces codes from it, and marking
    # it active before that check is how people lock themselves out.
    REQUIRES_ACTIVATION: ClassVar[bool]

    def __init__(self, db: AsyncSession, settings: ConfigServiceModel):
        self.db = db
        self.settings = settings

    @abstractmethod
    async def begin_enrollment(self, user: User, label: str) -> EnrollmentResult:
        """Create the credential row and return the one-time material."""

    async def complete_enrollment(self, credential: MfaCredential, code: str) -> None:
        """Prove the user can produce a code, and activate the credential.

        Concrete and refusing by default: a method with
        REQUIRES_ACTIVATION = False has nothing to prove, and forcing it to
        implement an empty override would be noise.
        """
        raise MfaErrors.activation_not_required()

    @abstractmethod
    async def verify(self, credential: MfaCredential, code: str, now: datetime) -> bool:
        """Whether `code` satisfies this credential right now.

        Mutates the credential's replay state on success. Returns False for
        every ordinary refusal — a wrong code, a spent backup code, a replayed
        step — so the caller can charge one failure and move on without
        learning which.
        """

    async def remaining(self, credential: MfaCredential) -> int | None:
        """How many uses are left, for methods where that is finite.

        None means "not a countable thing" — a TOTP seed does not run out.
        The UI uses this to warn before somebody's last backup code is spent.
        """
        return None
