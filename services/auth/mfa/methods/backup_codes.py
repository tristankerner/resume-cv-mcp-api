from __future__ import annotations

import hmac
import secrets
from datetime import datetime
from typing import ClassVar

from persistence.base import utcnow
from persistence.mfa_backup_code import MfaBackupCode
from persistence.mfa_credential import MfaCredential
from persistence.user import User
from services.auth.api_keys import ApiKeyToken
from services.auth.mfa.kinds import MfaMethodKind
from services.auth.mfa.methods.method import EnrollmentResult, MfaMethod


class BackupCodesMethod(MfaMethod):
    """A printed set of single-use recovery codes.

    The way back in when the authenticator is gone but the account is not.
    Generating a set replaces any previous one outright: two live sets is
    twice the secret material for no extra recovery.
    """

    KIND = MfaMethodKind.BACKUP_CODES
    LABEL = "Backup codes"
    ALLOWS_MULTIPLE = False
    REQUIRES_ACTIVATION = False

    CODE_BYTES: ClassVar[int] = 5  # 10 hex characters
    _SEPARATOR_INDEX: ClassVar[int] = 5

    async def begin_enrollment(self, user: User, label: str) -> EnrollmentResult:
        existing = await MfaCredential.list_for_user(self.db, user.id)
        for credential in existing:
            if credential.kind == str(self.KIND):
                await MfaBackupCode.delete_for_credential(self.db, credential.id)
                await self.db.delete(credential)
        await self.db.flush()

        credential = MfaCredential(
            user_id=user.id,
            kind=str(self.KIND),
            label=label,
            created_at=utcnow(),
            activated_at=utcnow(),  # nothing to prove
        )
        self.db.add(credential)
        await self.db.flush()

        codes = [
            self._generate_code() for _ in range(self.settings.mfa_backup_code_count)
        ]
        for code in codes:
            self.db.add(
                MfaBackupCode(
                    credential_id=credential.id,
                    code_hash=ApiKeyToken.hash_secret(self._normalise(code)),
                )
            )

        return EnrollmentResult(
            credential_id=credential.id, kind=self.KIND, label=label, codes=codes
        )

    async def verify(self, credential: MfaCredential, code: str, now: datetime) -> bool:
        normalised = self._normalise(code)
        if not normalised:
            return False
        target_hash = ApiKeyToken.hash_secret(normalised)

        for candidate in await MfaBackupCode.list_unused(self.db, credential.id):
            if not hmac.compare_digest(candidate.code_hash, target_hash):
                continue
            # Whether this code is still unspent is the database's answer,
            # not this row's — see MfaBackupCode.claim. A lost claim means a
            # concurrent request took the same code, which is a refusal
            # rather than a second success.
            if not await MfaBackupCode.claim(candidate, self.db, now):
                return False
            credential.last_used_at = now
            return True
        return False

    async def remaining(self, credential: MfaCredential) -> int | None:
        return await MfaBackupCode.count_unused(self.db, credential.id)

    def _generate_code(self) -> str:
        raw = secrets.token_hex(self.CODE_BYTES)
        return f"{raw[: self._SEPARATOR_INDEX]}-{raw[self._SEPARATOR_INDEX :]}"

    @staticmethod
    def _normalise(code: str) -> str:
        return code.strip().lower().replace("-", "").replace(" ", "")
