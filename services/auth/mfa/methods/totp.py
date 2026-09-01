from __future__ import annotations

import hmac
from datetime import UTC, datetime

import pyotp

from persistence.base import utcnow
from persistence.mfa_credential import MfaCredential
from persistence.user import User
from services.auth.exceptions import MfaErrors
from services.auth.mfa.kinds import MfaMethodKind
from services.auth.mfa.methods.method import EnrollmentResult, MfaMethod
from services.auth.mfa.secret_box import MfaSecretBox


class TotpMethod(MfaMethod):
    """RFC 6238 TOTP, six digits on a thirty-second step.

    The algorithm comes from pyotp; what is here is enrolment, the drift
    window, and the replay guard.

    The only class that handles a TOTP seed in the clear, and it holds one for
    the length of a method call — the column, the DTOs and the registry all
    see a sealed string. See MfaSecretBox.
    """

    KIND = MfaMethodKind.TOTP
    LABEL = "Authenticator app"
    ALLOWS_MULTIPLE = True
    REQUIRES_ACTIVATION = True

    def __init__(self, db, settings):
        super().__init__(db, settings)
        self.secret_box = MfaSecretBox.from_settings(settings)

    async def begin_enrollment(self, user: User, label: str) -> EnrollmentResult:
        secret = pyotp.random_base32()
        credential = MfaCredential(
            user_id=user.id,
            kind=str(self.KIND),
            label=label,
            secret=self.secret_box.seal(secret),
            created_at=utcnow(),
        )
        self.db.add(credential)
        await self.db.flush()  # so credential.id exists; the caller commits
        uri = pyotp.TOTP(secret).provisioning_uri(
            name=user.username, issuer_name=self.settings.mfa_issuer
        )
        return EnrollmentResult(
            credential_id=credential.id,
            kind=self.KIND,
            label=label,
            secret=secret,
            otpauth_uri=uri,
        )

    async def complete_enrollment(self, credential: MfaCredential, code: str) -> None:
        if credential.is_active:
            raise MfaErrors.already_activated()
        if not await self.verify(credential, code, utcnow()):
            raise MfaErrors.invalid_code()
        credential.activated_at = utcnow()

    async def verify(self, credential: MfaCredential, code: str, now: datetime) -> bool:
        if not credential.secret:
            return False
        cleaned = self._clean(code)
        # `isascii()` as well as `isdigit()`: the latter is True for
        # Arabic-Indic and full-width digits, and `hmac.compare_digest`
        # raises TypeError rather than returning False on a non-ASCII str —
        # which on this path is a 500 in the middle of a login.
        if not (cleaned.isascii() and cleaned.isdigit()):
            return False

        secret = self.secret_box.open(credential.secret)
        if secret is None:
            # No configured key opens the stored value. Refused rather than
            # raised: a login path that 500s tells an unauthenticated caller
            # more about the deployment than a refusal does. `verify_mfa_key`
            # is what catches this, at startup.
            return False

        # utcnow() is naive UTC by project convention (persistence/base.py) and
        # pyotp reads a naive datetime as local time. Converted once, so the
        # drift window and the recorded step cannot disagree by the local UTC
        # offset.
        aware_now = now.replace(tzinfo=UTC)
        totp = pyotp.TOTP(secret)
        drift = self.settings.mfa_totp_drift_steps
        for offset in range(-drift, drift + 1):
            expected = totp.at(aware_now, counter_offset=offset)
            if not hmac.compare_digest(expected, cleaned):
                continue
            step = totp.timecode(aware_now) + offset
            # The code matched; whether it is *spent* is the database's call.
            # `claim_totp_step` records the step only if nothing at or past it
            # is recorded already, which is what stops a replay from within the
            # same step, from walking backwards through the drift window, and
            # from a second request racing this one. The re-seal rides on the
            # same statement, so key rotation completes itself as people log in.
            return await MfaCredential.claim_totp_step(
                credential,
                self.db,
                step,
                now,
                self.secret_box.reseal(credential.secret),
            )
        return False

    @staticmethod
    def _clean(code: str) -> str:
        """Authenticator apps and the people reading them both add spaces."""
        return code.strip().replace(" ", "")
