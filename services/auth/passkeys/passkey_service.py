from typing import Annotated, ClassVar

from fastapi import Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.base import Base64Url, Clock
from persistence.passkey_credential import PasskeyCredential
from persistence.user import User
from services.auth.auth_service import AuthService
from services.auth.exceptions import PasskeyErrors
from services.auth.passkeys import ceremony as ceremony_module
from services.auth.passkeys.ceremony import PasskeyCeremony
from services.auth.passkeys.challenge import PasskeyRegistrationChallenge
from services.auth.passkeys.dtos.passkey import (
    PasskeyListResponse,
    PasskeyPasswordConfirmRequest,
    PasskeyRegistrationOptionsRequest,
    PasskeyRegistrationOptionsResponse,
    PasskeyRegistrationRequest,
    PasskeyRenameRequest,
    PasskeyStatus,
)
from services.auth.passkeys.relying_party import RelyingParty
from services.auth.principal import Principal
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService
from services.service_interface import ServiceProviderInterface
from services.user.exceptions import UserErrors


class PasskeyService(ServiceProviderInterface):
    """Registration, renaming and removal, for the authenticated owner of the
    account.

    Only ever acts on `principal.user_id`. Admin cleanup of somebody else's
    passkeys goes through `UserService.reset_passkeys`, the same split
    MfaService makes.
    """

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
        self.relying_party = RelyingParty(self.settings)
        self.ceremony = PasskeyCeremony(self.relying_party)

    async def list(self) -> PasskeyListResponse:
        """Does not require_enabled: a deployment that turns passkeys off
        after someone enrolled should still show them what they hold, so they
        can remove it."""
        self.principal.require_interactive()
        credentials = await PasskeyCredential.list_for_user(
            self.db, self.principal.user_id
        )
        return PasskeyListResponse(
            enabled=self.relying_party.enabled,
            credentials=[
                PasskeyStatus.model_validate(credential) for credential in credentials
            ],
        )

    async def registration_options(
        self, request: PasskeyRegistrationOptionsRequest
    ) -> PasskeyRegistrationOptionsResponse:
        self.principal.require_interactive()
        self.relying_party.require_enabled()
        user = await self._require_owner()
        await self.auth_service.verify_current_password(user, request.current_password)

        existing = await PasskeyCredential.list_for_user(self.db, user.id)
        if len(existing) >= self.MAX_CREDENTIALS:
            raise PasskeyErrors.too_many_credentials()

        handle = User.ensure_webauthn_handle(user)
        # `users` is audited, so a write to it has to state its own actor —
        # see ServiceProviderInterface.bind_audit_actor. A no-op on every call
        # after the first, since ensure_webauthn_handle only assigns once.
        await self.bind_audit_actor(self.db, self.principal)
        await self.db.commit()

        options, challenge = self.ceremony.registration_options(
            user, Base64Url.decode(handle), existing
        )
        token, expires_in = PasskeyRegistrationChallenge.mint(
            self.settings, user, challenge
        )
        return PasskeyRegistrationOptionsResponse(
            options=options, registration_token=token, expires_in=expires_in
        )

    async def register(self, request: PasskeyRegistrationRequest) -> PasskeyStatus:
        self.principal.require_interactive()
        self.relying_party.require_enabled()
        user = await self._require_owner()

        challenge = PasskeyRegistrationChallenge.redeem(
            self.settings, request.registration_token, user
        )
        if challenge is None:
            raise PasskeyErrors.challenge_expired()

        try:
            verified = self.ceremony.verify_registration(request.credential, challenge)
        except ceremony_module.RegistrationFailure as error:
            raise PasskeyErrors.rejected() from error

        transports = request.credential.get("response", {}).get("transports", [])
        credential = PasskeyCredential(
            user_id=user.id,
            credential_id=Base64Url.encode(verified.credential_id),
            public_key=Base64Url.encode(verified.credential_public_key),
            sign_count=verified.sign_count,
            transports=list(transports),
            aaguid=verified.aaguid,
            device_type=verified.credential_device_type.value,
            backed_up=verified.credential_backed_up,
            label=request.label,
            created_at=Clock.utcnow(),
        )
        self.db.add(credential)
        try:
            await self.db.commit()
        except IntegrityError as error:
            await self.db.rollback()
            raise PasskeyErrors.already_registered() from error

        return PasskeyStatus.model_validate(credential)

    async def rename(
        self, credential_id: int, request: PasskeyRenameRequest
    ) -> PasskeyStatus:
        """No password confirmation: a label is not a credential, and asking
        for one to fix a typo teaches people to type their password at
        prompts."""
        self.principal.require_interactive()
        credential = await PasskeyCredential.get_for_user(
            self.db, self.principal.user_id, credential_id
        )
        if credential is None:
            raise PasskeyErrors.not_found()

        credential.label = request.label
        await self.db.commit()
        return PasskeyStatus.model_validate(credential)

    async def remove(
        self, credential_id: int, request: PasskeyPasswordConfirmRequest
    ) -> None:
        """Password-confirmed, like MfaService.remove. Does not refuse to
        remove the last one — the password login still works, so this cannot
        lock anybody out."""
        self.principal.require_interactive()
        user = await self._require_owner()
        await self.auth_service.verify_current_password(user, request.current_password)

        credential = await PasskeyCredential.get_for_user(
            self.db, user.id, credential_id
        )
        if credential is None:
            raise PasskeyErrors.not_found()

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
    ) -> PasskeyService:
        return PasskeyService(db, principal, auth_service, config_service)
