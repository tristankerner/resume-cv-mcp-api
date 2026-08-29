"""Authorization codes: issuance, and the one-time redemption at /oauth/token.

S256 PKCE only. `plain` is never accepted; the
challenge method is not even a parameter here because there is only one.
"""

import base64
import hashlib
import hmac
import secrets
from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from persistence.base import utcnow
from persistence.oauth_authorization_code import TTL, OAuthAuthorizationCode
from services.auth.api_keys import ApiKeyToken
from services.auth.scopes import Scopes
from services.oauth.exceptions import OAuthErrors


class AuthorizationCodeStore:
    CODE_BYTES: ClassVar[int] = 32

    def __init__(self, db: AsyncSession):
        self.db = db

    @classmethod
    def _generate(cls) -> str:
        return secrets.token_urlsafe(cls.CODE_BYTES)

    @staticmethod
    def challenge_from_verifier(code_verifier: str) -> str:
        """RFC 7636 §4.2: BASE64URL-ENCODE(SHA256(ASCII(code_verifier)))."""
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    @classmethod
    def _verify_pkce(cls, code_verifier: str, code_challenge: str) -> bool:
        try:
            computed = cls.challenge_from_verifier(code_verifier)
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(computed, code_challenge)

    async def issue(
        self,
        *,
        client_id: str,
        user_id: int,
        redirect_uri: str,
        scopes: frozenset[Scopes],
        code_challenge: str,
        resource: str | None,
    ) -> str:
        raw_code = self._generate()
        record = OAuthAuthorizationCode(
            code_hash=ApiKeyToken.hash_secret(raw_code),
            client_id=client_id,
            user_id=user_id,
            redirect_uri=redirect_uri,
            scopes=sorted(str(scope) for scope in scopes),
            code_challenge=code_challenge,
            code_challenge_method="S256",
            resource=resource,
            expires_at=utcnow() + TTL,
        )
        self.db.add(record)
        await self.db.commit()
        return raw_code

    async def consume(
        self,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> OAuthAuthorizationCode:
        """Redeem a code exactly once.

        Raises `invalid_grant` for every failure mode alike — wrong client,
        wrong redirect_uri, expired, already used, or a verifier that does not
        hash to the stored challenge — without saying which: telling a caller
        which check failed is a free oracle onto a code it should not be able
        to validate.

        The row is locked for the caller's whole transaction (see
        `OAuthAuthorizationCode.lock_by_hash`), so two concurrent redemptions
        of the same code cannot both observe it as unconsumed; the caller
        commits once it has also written whatever the exchange produces, so
        the code's consumption is atomic with the tokens it grants.
        """
        record = await OAuthAuthorizationCode.lock_by_hash(
            self.db, ApiKeyToken.hash_secret(code)
        )
        if record is None or not record.is_usable():
            raise OAuthErrors.invalid_grant(
                "Authorization code is invalid, expired, or already used."
            )
        if record.client_id != client_id or record.redirect_uri != redirect_uri:
            raise OAuthErrors.invalid_grant(
                "Authorization code does not match this client or redirect_uri."
            )
        if not self._verify_pkce(code_verifier, record.code_challenge):
            raise OAuthErrors.invalid_grant(
                "code_verifier does not match the original code_challenge."
            )

        record.consumed_at = utcnow()
        return record
