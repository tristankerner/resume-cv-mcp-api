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

    The whole of the algorithm comes from pyotp; what is here is enrolment,
    the drift window, and the replay guard — the three things a library
    cannot decide for a deployment.

    The only class in the codebase that handles a TOTP seed in the clear, and
    it holds one for the length of a method call. Everything on either side —
    the column, the DTOs, the registry — sees either a sealed string or
    nothing at all. See MfaSecretBox.
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
            # The stored value will not open under any configured key. Refused
            # rather than raised: this is the wrong-key failure, and a login
            # path that 500s on it tells an unauthenticated caller more about
            # the deployment than a refusal does. `verify_mfa_key` is what is
            # supposed to catch this, at startup, before anyone tries.
            return False

        # utcnow() is naive UTC by project convention (persistence/base.py);
        # pyotp treats a naive datetime as local time, so it is made aware
        # here, once, and the same aware value feeds both calls below — using
        # a fresh conversion for each would let the drift window and the
        # recorded step disagree by the local UTC offset outside of UTC.
        aware_now = now.replace(tzinfo=UTC)
        totp = pyotp.TOTP(secret)
        drift = self.settings.mfa_totp_drift_steps
        for offset in range(-drift, drift + 1):
            expected = totp.at(aware_now, counter_offset=offset)
            if not hmac.compare_digest(expected, cleaned):
                continue
            step = totp.timecode(aware_now) + offset
            # The code matched; whether it is *spent* is decided by the
            # database, not here. `claim_totp_step` records the step only if
            # nothing at or past it is recorded already, so a code already
            # accepted cannot be presented again — not even within the same
            # thirty-second step it was minted for, not by walking backwards
            # inside the drift window, and not by a second request racing
            # this one. Its return is the verdict.
            #
            # The re-seal rides on the same statement, so key rotation
            # completes itself as people log in rather than needing a sweep.
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
