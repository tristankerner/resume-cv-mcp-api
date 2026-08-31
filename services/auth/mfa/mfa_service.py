from typing import Annotated, ClassVar, cast

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.mfa_backup_code import MfaBackupCode
from persistence.mfa_credential import MfaCredential
from persistence.user import User
from services.auth.auth_service import AuthService
from services.auth.exceptions import MfaErrors
from services.auth.mfa.dtos.mfa import (
    ActivateTotpRequest,
    AvailableMfaMethod,
    BackupCodesResponse,
    EnrollTotpRequest,
    EnrollTotpResponse,
    MfaCredentialStatus,
    MfaStatusResponse,
    PasswordConfirmRequest,
)
from services.auth.mfa.kinds import MfaMethodKind
from services.auth.mfa.registry import MfaMethodRegistry
from services.auth.principal import Principal
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService
from services.service_interface import ServiceProviderInterface
from services.user.exceptions import UserErrors


class MfaService(ServiceProviderInterface):
    """Enrolment and removal, for the authenticated owner of the account.

    Only ever acts on `principal.user_id`. There is no admin path through
    here — an admin clearing somebody else's factors goes through
    `UserService.reset_mfa`, which is a different operation with a different
    justification and deserves to be findable as one.
    """

    # Bounds enrolment as an unbounded write for whoever holds a session,
    # not a real limit anyone should reach.
    MAX_CREDENTIALS: ClassVar[int] = 10

    def __init__(
        self,
        db: AsyncSession,
        principal: Principal,
        auth_service: AuthService,
        config_service: ConfigService,
    ):
        self.db = db
        self.principal = principal
        self.auth_service = auth_service
        self.settings = config_service.settings
        self.registry = MfaMethodRegistry(db, self.settings)

    async def status(self) -> MfaStatusResponse:
        self.principal.require_interactive()
        credentials = await MfaCredential.list_for_user(self.db, self.principal.user_id)

        statuses: list[MfaCredentialStatus] = []
        backup_codes_remaining: int | None = None
        any_active = False
        for credential in credentials:
            method = self.registry.for_credential(credential)
            if method is None:
                continue
            remaining = await method.remaining(credential)
            if credential.kind == str(MfaMethodKind.BACKUP_CODES):
                backup_codes_remaining = remaining
            if credential.is_active:
                any_active = True
            statuses.append(
                MfaCredentialStatus(
                    id=credential.id,
                    kind=MfaMethodKind(credential.kind),
                    label=credential.label,
                    created_at=credential.created_at,
                    activated_at=credential.activated_at,
                    last_used_at=credential.last_used_at,
                    remaining=remaining,
                )
            )

        return MfaStatusResponse(
            enrolled=any_active,
            credentials=statuses,
            available_methods=[
                AvailableMfaMethod(
                    kind=cast(str, entry["kind"]),
                    label=cast(str, entry["label"]),
                    allows_multiple=cast(bool, entry["allows_multiple"]),
                )
                for entry in MfaMethodRegistry.describe()
            ],
            backup_codes_remaining=backup_codes_remaining,
        )

    async def enroll_totp(self, request: EnrollTotpRequest) -> EnrollTotpResponse:
        self.principal.require_interactive()
        user = await self._require_owner()
        await self.auth_service.verify_current_password(user, request.current_password)

        existing = await MfaCredential.list_for_user(self.db, user.id)
        if len(existing) >= self.MAX_CREDENTIALS:
            raise MfaErrors.too_many_credentials()

        method = self.registry.for_kind(MfaMethodKind.TOTP)
        result = await method.begin_enrollment(user, request.label)
        await self.db.commit()

        return EnrollTotpResponse(
            credential_id=result.credential_id,
            kind=result.kind,
            label=result.label,
            secret=cast(str, result.secret),
            otpauth_uri=cast(str, result.otpauth_uri),
        )

    async def activate_totp(
        self, credential_id: int, request: ActivateTotpRequest
    ) -> None:
        self.principal.require_interactive()
        credential = await MfaCredential.get_for_user(
            self.db, self.principal.user_id, credential_id
        )
        if credential is None:
            raise MfaErrors.credential_not_found()
        if credential.kind != str(MfaMethodKind.TOTP):
            raise MfaErrors.wrong_kind()

        method = self.registry.for_kind(MfaMethodKind.TOTP)
        # Not charged to the login throttle: the caller is already
        # authenticated and holds no advantage from guessing at a secret
        # they were just handed.
        await method.complete_enrollment(credential, request.code)
        await self.db.commit()

    async def regenerate_backup_codes(
        self, request: PasswordConfirmRequest
    ) -> BackupCodesResponse:
        """Also how backup codes are first created — one route, not an
        enrol/regenerate pair, because BackupCodesMethod.ALLOWS_MULTIPLE is
        False and makes the two operations identical."""
        self.principal.require_interactive()
        user = await self._require_owner()
        await self.auth_service.verify_current_password(user, request.current_password)

        method = self.registry.for_kind(MfaMethodKind.BACKUP_CODES)
        result = await method.begin_enrollment(user, method.LABEL)
        await self.db.commit()

        return BackupCodesResponse(
            credential_id=result.credential_id,
            kind=result.kind,
            codes=cast(list[str], result.codes),
        )

    async def remove(self, credential_id: int, request: PasswordConfirmRequest) -> None:
        """Does not refuse to remove the last factor: MFA is optional, so a
        user may turn it off entirely. The client warns; the API does not
        block."""
        self.principal.require_interactive()
        user = await self._require_owner()
        await self.auth_service.verify_current_password(user, request.current_password)

        credential = await MfaCredential.get_for_user(self.db, user.id, credential_id)
        if credential is None:
            raise MfaErrors.credential_not_found()

        await MfaBackupCode.delete_for_credential(self.db, credential.id)
        await self.db.delete(credential)
        await self.db.commit()

    async def _require_owner(self) -> User:
        user = await User.get_user_by_id(self.db, self.principal.user_id)
        if user is None:
            raise UserErrors.not_found()
        return user

    @staticmethod
    def get_with_deps(
        db: Annotated[AsyncSession, Depends(DatabaseService.get_async_db_session)],
        principal: Annotated[Principal, Depends(AuthService.get_principal)],
        auth_service: Annotated[AuthService, Depends(AuthService.get_with_deps)],
        config_service: Annotated[ConfigService, Depends(ConfigService.get_with_deps)],
    ) -> MfaService:
        return MfaService(db, principal, auth_service, config_service)
