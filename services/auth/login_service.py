from datetime import timedelta
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.user import User
from services.auth.auth_service import AuthService
from services.auth.dtos.capabilities import AuthCapabilities
from services.auth.dtos.mfa import MfaRequiredResponse
from services.auth.exceptions import AuthErrors
from services.auth.mfa.challenge import MfaChallengeContext, MfaChallengeToken
from services.auth.mfa.verifier import MfaVerifier
from services.auth.passkeys.challenge import PasskeyContext
from services.auth.passkeys.dtos.passkey import (
    PasskeyAuthenticationOptionsRequest,
    PasskeyAuthenticationOptionsResponse,
    PasskeyAuthenticationRequest,
)
from services.auth.passkeys.login import PasskeyLogin
from services.auth.refresh_tokens import RefreshTokenIssuer
from services.auth.token_data import Token
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService
from services.service_interface import ServiceProviderInterface


class LoginService(ServiceProviderInterface):
    """The password login, both steps.

    Owns the branch that /token used to make inline: password accepted plus
    no second factor is a token, password accepted plus a second factor is a
    challenge, and redeeming that challenge is the same token by a different
    door.
    """

    def __init__(
        self, db: AsyncSession, auth_service: AuthService, config_service: ConfigService
    ):
        self.db = db
        self.auth_service = auth_service
        self.config_service = config_service
        self.settings = config_service.settings

    def capabilities(self) -> AuthCapabilities:
        """What this deployment's auth surface supports. Anonymous and cheap
        — the browser client calls it before login to decide whether to show
        a passkey button, so it must not itself require one."""
        return AuthCapabilities(passkeys=self.settings.passkeys_enabled)

    async def passkey_options(
        self, request: PasskeyAuthenticationOptionsRequest
    ) -> PasskeyAuthenticationOptionsResponse:
        return await PasskeyLogin(self.db, self.settings).options(
            request.username, PasskeyContext.TOKEN
        )

    async def passkey_login(self, request: PasskeyAuthenticationRequest) -> Token:
        # No MfaVerifier, no MfaRequiredResponse branch, unlike `login` above:
        # a passkey already proves possession (the private key) and knowledge
        # or inherence (the PIN or biometric that unlocked it), so it is a
        # complete login on its own. Demanding a TOTP code afterwards would
        # make the stronger credential the more annoying one — see
        # services/auth/passkeys/login.py's docstring.
        user = await PasskeyLogin(self.db, self.settings).authenticate(
            self.auth_service,
            request.login_token,
            request.credential,
            PasskeyContext.TOKEN,
        )
        return await self._issue(user)

    async def login(self, username: str, password: str) -> Token | MfaRequiredResponse:
        user = await self.auth_service.authenticate_user(username, password)
        if not user:
            raise AuthErrors.credentials()

        verifier = MfaVerifier(self.db, self.settings)
        kinds = await verifier.required_kinds(user)
        if not kinds:
            return await self._issue(user)

        token, expires_in = MfaChallengeToken.mint(
            self.settings, user, MfaChallengeContext.TOKEN
        )
        return MfaRequiredResponse(
            mfa_token=token, methods=kinds, expires_in=expires_in
        )

    async def complete_mfa(self, mfa_token: str, code: str) -> Token:
        verifier = MfaVerifier(self.db, self.settings)
        user = await verifier.user_from_challenge(mfa_token, MfaChallengeContext.TOKEN)
        if user is None:
            raise AuthErrors.credentials()

        self.auth_service.raise_if_locked(user)

        if not await verifier.verify(user, code):
            await self.auth_service.register_mfa_failure(user)
            raise AuthErrors.credentials()
        await self.auth_service.clear_login_failures(user)
        return await self._issue(user)

    async def refresh(self, refresh_token: str) -> Token:
        """Exchange a refresh token for a new access token and a new refresh
        token, without a password or an MFA code — the refresh token is only
        ever handed out after both have already been satisfied for this
        session."""
        user, new_refresh_token = await RefreshTokenIssuer(
            self.db, self.config_service
        ).rotate(presented_token=refresh_token)
        return self._issue_access_token(user, new_refresh_token)

    async def logout(self, refresh_token: str) -> None:
        """Revoke the whole session chain the presented token belongs to.

        The access token already issued for this session is a stateless JWT
        and keeps working until its own `exp` — logout only stops the session
        from being refreshed past that point.
        """
        await RefreshTokenIssuer(self.db, self.config_service).logout(
            presented_token=refresh_token
        )

    def _issue_access_token(self, user: User, refresh_token: str) -> Token:
        access_token_expires = timedelta(
            minutes=self.settings.auth_access_token_expire_minutes
        )
        access_token = self.auth_service.create_access_token(
            data={"sub": str(user.id)}, expires_delta=access_token_expires
        )
        return Token(  # nosec B106
            access_token=access_token,
            token_type="bearer",
            refresh_token=refresh_token,
        )

    async def _issue(self, user: User) -> Token:
        user_agent = (
            self.auth_service.request.headers.get("user-agent")
            if self.auth_service.request
            else None
        )
        refresh_token = await RefreshTokenIssuer(self.db, self.config_service).issue(
            user_id=user.id, user_agent=user_agent
        )
        return self._issue_access_token(user, refresh_token)

    @staticmethod
    def get_with_deps(
        db: Annotated[AsyncSession, Depends(DatabaseService.get_async_db_session)],
        auth_service: Annotated[AuthService, Depends(AuthService.get_with_deps)],
        config_service: Annotated[ConfigService, Depends(ConfigService.get_with_deps)],
    ) -> LoginService:
        return LoginService(db, auth_service, config_service)
