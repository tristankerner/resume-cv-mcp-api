from __future__ import annotations

import secrets
from enum import StrEnum
from typing import ClassVar

from persistence.user import User
from services.auth.signed_token import SignedToken
from services.config.config_service import ConfigServiceModel


class MfaChallengeContext(StrEnum):
    """Which surface minted a challenge.

    A challenge issued by the docs login must not be redeemable at /token,
    and vice versa: the three surfaces grant different things, and a token
    that works everywhere would let the weakest of them stand in for the
    strongest.
    """

    TOKEN = "token"  # nosec B105 - names the /token surface, not a credential
    OAUTH = "oauth"
    DOCS = "docs"


class MfaChallengeToken(SignedToken):
    """Mint and verify the short-lived proof that a password was accepted.

    A signed JWT and not a database row: nothing needs revoking (it lives
    five minutes), nothing needs pruning, and a scale-to-zero deployment pays
    no write for a login that has not finished. Bound to the password hash in
    force when it was minted, so changing the password invalidates every
    challenge outstanding against the old one.
    """

    TOKEN_USE: ClassVar[str] = "mfa_pending"

    @classmethod
    def mint(
        cls,
        settings: ConfigServiceModel,
        user: User,
        context: MfaChallengeContext,
        binding: str | None = None,
    ) -> tuple[str, int]:
        """`binding` narrows a challenge below its context, for a surface
        where the context alone is too wide.

        The OAuth flow passes the `client_id`: without it a challenge issued
        while authorizing one client could be redeemed while authorizing
        another, and the consent a user gave on the first page is not consent
        to whatever the second one asked for. The other two surfaces have
        nothing to narrow, and pass None.
        """
        return cls._encode(
            settings,
            {
                "sub": str(user.id),
                "ctx": str(context),
                "bnd": binding,
                "pwb": cls.password_binding(user),
                # For log correlation only; nothing checks this back.
                "jti": secrets.token_urlsafe(8),
            },
            settings.mfa_challenge_ttl_minutes,
        )

    @classmethod
    def _validated_payload(
        cls,
        settings: ConfigServiceModel,
        token: str,
        context: MfaChallengeContext,
        binding: str | None = None,
    ) -> dict | None:
        """Decode, and reject anything not minted for `context` and `binding`.

        Shared by `verify` and `MfaVerifier.user_from_challenge`, which also
        needs the `pwb` claim — a check `verify`'s `int | None` return
        cannot carry, since it needs the user row loaded first.
        """
        payload = cls._decode(settings, token)
        if payload is None:
            return None
        if payload.get("ctx") != str(context):
            return None
        # An absent claim reads as None, so a token minted before this
        # existed fails against a caller that expects a binding rather than
        # passing unchecked.
        if payload.get("bnd") != binding:
            return None
        return payload

    @classmethod
    def verify(
        cls, settings: ConfigServiceModel, token: str, context: MfaChallengeContext
    ) -> int | None:
        """Decode, reject the wrong context, and return the user id.

        Returns None for every failure (expired, wrong signature, wrong
        context, malformed) — callers turn that into one indistinguishable
        401. Does not check the password binding; that needs the user row,
        so it happens in `MfaVerifier.user_from_challenge`.
        """
        payload = cls._validated_payload(settings, token, context)
        if payload is None:
            return None
        subject = payload.get("sub")
        if not isinstance(subject, str):
            return None
        try:
            return int(subject)
        except ValueError:
            return None
