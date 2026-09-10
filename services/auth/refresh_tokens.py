"""Session refresh tokens for the interactive login (`/token`, `/token/mfa`):
opaque, hashed, rotated with reuse detection. Modelled on
`services.oauth.tokens.TokenIssuer` and `persistence.oauth_refresh_token
.OAuthRefreshToken` — nearly every decision here repeats one already made
there, keyed on `session_id` instead of an OAuth `grant_id` since there is no
client to scope this to.

MFA is deliberately not re-challenged on refresh: a refresh token is only ever
issued after MFA has already been satisfied for that login, and re-challenging
here would defeat the point of the feature.
"""

import logging
import secrets
from datetime import timedelta
from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from persistence.auth_refresh_token import AuthRefreshToken
from persistence.base import Clock
from persistence.user import User
from services.auth.api_keys import ApiKeyToken
from services.auth.auth_service import AuthService
from services.auth.exceptions import AuthErrors
from services.config.config_service import ConfigService


class RefreshTokenIssuer:
    LOG: ClassVar[logging.Logger] = logging.getLogger("uvicorn")
    REFRESH_TOKEN_BYTES: ClassVar[int] = 32

    def __init__(self, db: AsyncSession, config_service: ConfigService):
        self.db = db
        self.config_service = config_service

    @property
    def _ttl(self) -> timedelta:
        return timedelta(
            days=self.config_service.settings.auth_refresh_token_expire_days
        )

    @classmethod
    def _generate_refresh_token(cls) -> str:
        return secrets.token_urlsafe(cls.REFRESH_TOKEN_BYTES)

    async def issue(self, *, user_id: int, user_agent: str | None) -> str:
        """A refresh token starting a fresh rotation chain, minted alongside
        an access token at the end of login."""
        raw_token = self._generate_refresh_token()
        record = AuthRefreshToken(
            token_hash=ApiKeyToken.hash_secret(raw_token),
            user_id=user_id,
            session_id=secrets.token_hex(16),
            expires_at=Clock.utcnow() + self._ttl,
            user_agent=user_agent,
        )
        self.db.add(record)
        await self.db.commit()
        return raw_token

    async def rotate(self, *, presented_token: str) -> tuple[User, str]:
        """Redeem a refresh token for a new one and the user it belongs to,
        revoking the one presented.

        Re-presenting an already-revoked token is reuse, and RFC 6819's answer
        is to revoke the whole chain rather than trust either party's copy
        from here on. That case, an unknown token, and a merely expired one
        all raise the same `AuthErrors.credentials()`: telling them apart
        would tell an attacker which guess was closer.

        The row is locked before any of these checks (see
        `AuthRefreshToken.lock_by_hash`), so two concurrent uses of the same
        token cannot both observe it as live.

        Refuses without mutating anything for a locked or deactivated account:
        unlike reuse, that is not evidence the token itself was compromised,
        so the token stays live for whenever the account is usable again.
        """
        record = await AuthRefreshToken.lock_by_hash(
            self.db, ApiKeyToken.hash_secret(presented_token)
        )
        if record is None:
            self.LOG.warning("Refresh grant rejected: unknown token.")
            raise AuthErrors.credentials()

        if record.revoked_at is not None:
            self.LOG.warning(
                "Refresh token reuse detected: session_id=%s — revoking the "
                "whole session chain.",
                record.session_id,
            )
            await AuthRefreshToken.revoke_chain(self.db, record.session_id)
            raise AuthErrors.credentials()
        if record.expires_at <= Clock.utcnow():
            self.LOG.info(
                "Refresh grant rejected: token expired (session_id=%s).",
                record.session_id,
            )
            raise AuthErrors.credentials()

        user = await User.get_user_by_id(self.db, record.user_id)
        if user is None or not user.active:
            raise AuthErrors.credentials()

        # Same check LoginService.complete_mfa runs before issuing a token —
        # an account locked after the refresh token was issued must stop
        # refreshing. Raises AuthErrors.account_locked* without mutating
        # `record`, on purpose: see the docstring above.
        AuthService(self.db, None, self.config_service).raise_if_locked(user)

        new_raw = self._generate_refresh_token()
        new_record = AuthRefreshToken(
            token_hash=ApiKeyToken.hash_secret(new_raw),
            user_id=record.user_id,
            session_id=record.session_id,
            expires_at=Clock.utcnow() + self._ttl,
            user_agent=record.user_agent,
        )
        self.db.add(new_record)
        await self.db.flush()
        record.revoked_at = Clock.utcnow()
        record.rotated_to_id = new_record.id
        record.last_used_at = Clock.utcnow()
        await self.db.commit()
        self.LOG.info("Refresh token rotated: session_id=%s.", record.session_id)
        return user, new_raw

    async def logout(self, *, presented_token: str) -> None:
        """Revoke the whole session chain. Idempotent and silent about
        whether the token existed — same reasoning as
        `TokenIssuer.revoke`."""
        record = await AuthRefreshToken.lock_by_hash(
            self.db, ApiKeyToken.hash_secret(presented_token)
        )
        if record is None:
            return
        await AuthRefreshToken.revoke_chain(self.db, record.session_id)
