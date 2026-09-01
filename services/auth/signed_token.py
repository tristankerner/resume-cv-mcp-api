from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import jwt
from jwt.exceptions import InvalidTokenError

from persistence.user import User
from services.config.config_service import ConfigServiceModel


class SignedToken:
    """Base for the two JWTs this service mints that are not access tokens:
    the MFA challenge (services/auth/mfa/challenge.py) and the docs-session
    cookie (services/auth/docs_session.py).

    Both are stateless — nothing to revoke, nothing to prune — and both are
    bound to the password hash in force when minted, so a password change
    invalidates every one outstanding. Both must also be refused if presented
    as `Authorization: Bearer ...`, which is `AuthService._authenticate_jwt`'s
    `token_use` guard; keeping the claim and the encode/decode path here is
    what stops a new subclass from forgetting to set it.
    """

    TOKEN_USE: ClassVar[str]

    @classmethod
    def _encode(
        cls, settings: ConfigServiceModel, claims: dict, ttl_minutes: int
    ) -> tuple[str, int]:
        now = datetime.now(UTC)
        payload = {
            **claims,
            "token_use": cls.TOKEN_USE,
            "iat": now,
            "exp": now + timedelta(minutes=ttl_minutes),
        }
        token = jwt.encode(
            payload,
            settings.auth_secret_key.get_secret_value(),
            algorithm=settings.auth_algorithm,
        )
        return token, ttl_minutes * 60

    @classmethod
    def _decode(cls, settings: ConfigServiceModel, token: str) -> dict | None:
        try:
            payload = jwt.decode(
                token,
                settings.auth_secret_key.get_secret_value(),
                algorithms=[str(settings.auth_algorithm)],
            )
        except InvalidTokenError:
            return None
        if payload.get("token_use") != cls.TOKEN_USE:
            return None
        return payload

    @staticmethod
    def password_binding(user: User) -> str:
        """A fingerprint of the password hash in force right now, so a token
        minted against an old password stops working the moment it changes."""
        return hashlib.sha256((user.password or "").encode()).hexdigest()[:32]
