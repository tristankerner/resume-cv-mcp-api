from __future__ import annotations

import secrets
from enum import StrEnum
from typing import ClassVar

from persistence.base import Base64Url
from persistence.user import User
from services.auth.signed_token import SignedToken
from services.config.config_service import ConfigServiceModel


class PasskeyContext(StrEnum):
    """Which surface minted a challenge. Same rule as MfaChallengeContext: a
    challenge issued by the docs login must not be redeemable at /token."""

    TOKEN = "token"  # nosec B105 - names the /token surface, not a credential
    OAUTH = "oauth"
    DOCS = "docs"


class PasskeyRegistrationChallenge(SignedToken):
    """Issued by POST /users/me/passkeys/options, redeemed by POST
    /users/me/passkeys.

    Carries `pwb` because registration is gated on the current password: a
    token minted before a password change must not still be redeemable after
    it, exactly as for MfaChallengeToken.
    """

    TOKEN_USE: ClassVar[str] = "passkey_register"

    @classmethod
    def mint(
        cls, settings: ConfigServiceModel, user: User, challenge: bytes
    ) -> tuple[str, int]:
        return cls._encode(
            settings,
            {
                "sub": str(user.id),
                "chal": Base64Url.encode(challenge),
                "pwb": cls.password_binding(user),
            },
            settings.webauthn_challenge_ttl_minutes,
        )

    @classmethod
    def redeem(
        cls, settings: ConfigServiceModel, token: str, user: User
    ) -> bytes | None:
        """The challenge bytes, or None for any failure at all — expired,
        wrong signature, minted for another account, or minted against a
        password that has since changed."""
        payload = cls._decode(settings, token)
        if payload is None:
            return None
        if payload.get("sub") != str(user.id):
            return None
        if payload.get("pwb") != cls.password_binding(user):
            return None
        raw_challenge = payload.get("chal")
        if not isinstance(raw_challenge, str):
            return None
        return Base64Url.decode(raw_challenge)


class PasskeyAuthenticationChallenge(SignedToken):
    """Issued by one of the three `…/passkey/options` routes, redeemed by its
    matching submit route.

    No `pwb`: no password is involved on this path, and binding one would
    mean a user who has forgotten their password could not use the
    credential that exists so they do not need it.

    `sub` is the user id when the ceremony named a username, and absent for
    the usernameless (discoverable-credential) flow. When present it is
    enforced: a challenge issued for one account must not be redeemed with
    another account's credential.
    """

    TOKEN_USE: ClassVar[str] = "passkey_auth"

    @classmethod
    def mint(
        cls,
        settings: ConfigServiceModel,
        challenge: bytes,
        user_id: int | None,
        context: PasskeyContext,
        binding: str | None = None,
    ) -> tuple[str, int]:
        claims = {
            "chal": Base64Url.encode(challenge),
            "ctx": str(context),
            "bnd": binding,
            "jti": secrets.token_urlsafe(8),
        }
        # Omitted rather than set to `None`: PyJWT validates a *present* `sub`
        # claim as a registered claim and rejects one that is not a string,
        # even when the value is `null` — so a discoverable-credential
        # challenge, which has no subject to carry, must leave the key out
        # entirely rather than carry it empty.
        if user_id is not None:
            claims["sub"] = str(user_id)
        return cls._encode(settings, claims, settings.webauthn_challenge_ttl_minutes)

    @classmethod
    def redeem(
        cls,
        settings: ConfigServiceModel,
        token: str,
        context: PasskeyContext,
        binding: str | None = None,
    ) -> tuple[bytes, int | None] | None:
        """(challenge, user_id-or-None), or None for any failure."""
        payload = cls._decode(settings, token)
        if payload is None:
            return None
        if payload.get("ctx") != str(context):
            return None
        # An absent claim reads as None, so a token minted before this
        # existed fails against a caller that expects a binding rather than
        # passing unchecked. See MfaChallengeToken._validated_payload.
        if payload.get("bnd") != binding:
            return None

        raw_challenge = payload.get("chal")
        if not isinstance(raw_challenge, str):
            return None
        challenge = Base64Url.decode(raw_challenge)

        subject = payload.get("sub")
        if subject is None:
            return challenge, None
        if not isinstance(subject, str):
            return None
        try:
            user_id = int(subject)
        except ValueError:
            return None
        return challenge, user_id
