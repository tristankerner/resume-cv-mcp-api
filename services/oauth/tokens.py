"""Access tokens (JWT, stateless) and refresh tokens (opaque, stored hashed,
rotated with reuse detection). See AuthService.authenticate for the
compatibility contract these are built to keep."""

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.base import utcnow
from persistence.oauth_refresh_token import TTL as REFRESH_TTL
from persistence.oauth_refresh_token import OAuthRefreshToken
from services.auth.api_keys import ApiKeyToken
from services.auth.scopes import Scopes
from services.config.config_service import ConfigServiceModel
from services.oauth.exceptions import OAuthErrors

log = logging.getLogger("uvicorn")


class TokenIssuer:
    REFRESH_TOKEN_BYTES: ClassVar[int] = 32

    def __init__(self, db: AsyncSession, settings: ConfigServiceModel):
        self.db = db
        self.settings = settings

    def mint_access_token(
        self,
        *,
        user_id: int,
        scopes: frozenset[Scopes],
        client_id: str,
    ) -> tuple[str, int]:
        """A JWT access token, discriminated from a password JWT by `token_use`.

        Signed with the same key and algorithm as every other token this
        service issues; `AuthService._authenticate_oauth` dispatches on
        `token_use` rather than on how the signature was produced.

        Shaped to RFC 9068, which makes `iss`, `exp`, `aud`, `sub`,
        `client_id`, `iat` and `jti` REQUIRED and the `typ` header `at+jwt`. A
        client that checks conformance refuses a non-conforming token rather
        than presenting it, which reads in the logs as a successful exchange
        followed by silence.

        `iss` is rendered exactly as the authorization server metadata renders
        it, trailing slash included, so a client comparing the two finds them
        equal.
        """
        settings = self.settings
        expires_delta = timedelta(minutes=settings.auth_access_token_expire_minutes)
        now = datetime.now(UTC)
        payload = {
            "iss": str(settings.public_base_url),
            "sub": str(user_id),
            "scope": " ".join(sorted(str(scope) for scope in scopes)),
            "aud": settings.oauth_resource_url,
            "client_id": client_id,
            "token_use": "oauth_access",  # nosec B105
            "iat": now,
            "exp": now + expires_delta,
            "jti": secrets.token_hex(16),
        }
        token = jwt.encode(
            payload,
            settings.auth_secret_key.get_secret_value(),
            algorithm=settings.auth_algorithm,
            headers={"typ": "at+jwt"},
        )
        return token, int(expires_delta.total_seconds())

    @classmethod
    def _generate_refresh_token(cls) -> str:
        return secrets.token_urlsafe(cls.REFRESH_TOKEN_BYTES)

    async def issue_refresh_token(
        self,
        *,
        client_id: str,
        user_id: int,
        scopes: frozenset[Scopes],
        resource: str | None,
    ) -> str:
        """A refresh token starting a fresh grant chain."""
        raw_token = self._generate_refresh_token()
        record = OAuthRefreshToken(
            token_hash=ApiKeyToken.hash_secret(raw_token),
            client_id=client_id,
            user_id=user_id,
            scopes=sorted(str(scope) for scope in scopes),
            resource=resource,
            grant_id=secrets.token_hex(16),
            expires_at=utcnow() + REFRESH_TTL,
        )
        self.db.add(record)
        await self.db.commit()
        return raw_token

    async def rotate(
        self, *, presented_token: str, client_id: str
    ) -> tuple[OAuthRefreshToken, str]:
        """Redeem a refresh token for a new one, revoking the one presented.

        Re-presenting an already-revoked token is reuse, and RFC 6819's answer
        is to revoke the whole chain rather than trust either party's copy from
        here on. That case, an unknown token, and a merely expired one all
        raise the same `invalid_grant`: telling them apart would tell an
        attacker which guess was closer.

        The row is locked before any of these checks (see
        `OAuthRefreshToken.lock_by_hash`), so two concurrent uses of the same
        token cannot both observe it as live.

        Every outcome is logged server-side with which of the four rejection
        reasons applied — the distinction the caller is deliberately not given
        is exactly the one an operator needs when diagnosing frequent
        re-authentication.
        """
        record = await OAuthRefreshToken.lock_by_hash(
            self.db, ApiKeyToken.hash_secret(presented_token)
        )
        if record is None:
            log.warning(
                "OAuth refresh grant rejected: unknown token (client_id=%s).",
                client_id,
            )
            raise OAuthErrors.invalid_grant("Refresh token is invalid.")
        if record.client_id != client_id:
            log.warning(
                "OAuth refresh grant rejected: token belongs to client_id=%s, not "
                "the presenting client_id=%s.",
                record.client_id,
                client_id,
            )
            raise OAuthErrors.invalid_grant(
                "Refresh token does not belong to this client."
            )

        if record.revoked_at is not None:
            log.warning(
                "OAuth refresh token reuse detected: client_id=%s grant_id=%s — "
                "revoking the whole grant chain.",
                record.client_id,
                record.grant_id,
            )
            await OAuthRefreshToken.revoke_chain(self.db, record.grant_id)
            raise OAuthErrors.invalid_grant("Refresh token has already been used.")
        if record.expires_at <= utcnow():
            log.info(
                "OAuth refresh grant rejected: token expired (client_id=%s "
                "grant_id=%s).",
                record.client_id,
                record.grant_id,
            )
            raise OAuthErrors.invalid_grant("Refresh token has expired.")

        new_raw = self._generate_refresh_token()
        new_record = OAuthRefreshToken(
            token_hash=ApiKeyToken.hash_secret(new_raw),
            client_id=record.client_id,
            user_id=record.user_id,
            scopes=list(record.scopes),
            resource=record.resource,
            grant_id=record.grant_id,
            expires_at=utcnow() + REFRESH_TTL,
        )
        self.db.add(new_record)
        await self.db.flush()
        record.revoked_at = utcnow()
        record.rotated_to_id = new_record.id
        await self.db.commit()
        log.info(
            "OAuth refresh token rotated: client_id=%s grant_id=%s.",
            record.client_id,
            record.grant_id,
        )
        return new_record, new_raw

    async def revoke(self, *, presented_token: str, client_id: str) -> None:
        """RFC 7009: revoke a refresh token. Idempotent, and silent about
        whether the token existed at all — a caller revoking a token that is
        unknown, already revoked, or belongs to someone else all see success,
        per RFC 7009 §2.2's guidance not to leak that distinction."""
        record = await OAuthRefreshToken.lock_by_hash(
            self.db, ApiKeyToken.hash_secret(presented_token)
        )
        if record is None or record.client_id != client_id:
            return
        if record.revoked_at is None:
            record.revoked_at = utcnow()
            await self.db.commit()
