"""Dynamic Client Registration (RFC 7591), and lookups against what it wrote.

Registration is deliberately credential-free — see redirect_allowlist.py for
what bounds it instead — so nothing here checks who is registering, only
whether every `redirect_uris` entry is one the allowlist permits.
"""

import secrets
from datetime import UTC
from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from persistence.oauth_client import OAuthClient
from services.auth.api_keys import ApiKeyToken
from services.oauth.dtos import ClientRegistrationRequest, ClientRegistrationResponse
from services.oauth.exceptions import OAuthErrors
from services.oauth.redirect_allowlist import RedirectAllowlist


class OAuthClientRegistry:
    CLIENT_ID_BYTES: ClassVar[int] = 16
    CLIENT_SECRET_BYTES: ClassVar[int] = 32

    def __init__(self, db: AsyncSession):
        self.db = db

    @classmethod
    def _generate_client_id(cls) -> str:
        return secrets.token_urlsafe(cls.CLIENT_ID_BYTES)

    @classmethod
    def _generate_client_secret(cls) -> str:
        return secrets.token_urlsafe(cls.CLIENT_SECRET_BYTES)

    async def create(
        self,
        *,
        redirect_uris: list[str],
        client_name: str | None,
        token_endpoint_auth_method: str,
        scope: str | None,
        grant_types: list[str],
        response_types: list[str],
        allowed_hosts: frozenset[str],
    ) -> tuple[OAuthClient, str | None]:
        """Create a client row, returning it with its secret if it has one.

        The single place a client is created, shared by anonymous DCR and by
        `python -m register_oauth_client`. Pre-registration is the normal path
        — `OAUTH_REGISTRATION_ENABLED` is off by default — and it goes through
        the same validation deliberately: a redirect URI typed at a terminal
        deserves the allowlist check just as much as one arriving over HTTP,
        and having one caller skip it is how the two drift apart.

        The secret is returned rather than stored in the clear, and is the
        only time it exists in a readable form.
        """
        allowlist = RedirectAllowlist(allowed_hosts)
        for uri in redirect_uris:
            if not allowlist.allows(uri):
                raise OAuthErrors.invalid_redirect_uri(
                    f"{uri!r} is not an allowed redirect host"
                )

        if "code" not in response_types:
            raise OAuthErrors.invalid_client_metadata(
                "response_types must include 'code'"
            )
        if "authorization_code" not in grant_types:
            raise OAuthErrors.invalid_client_metadata(
                "grant_types must include 'authorization_code'"
            )

        client_id = self._generate_client_id()
        raw_secret: str | None = None
        secret_hash: str | None = None
        if token_endpoint_auth_method == "client_secret_post":  # nosec B105
            raw_secret = self._generate_client_secret()
            secret_hash = ApiKeyToken.hash_secret(raw_secret)

        client = OAuthClient(
            client_id=client_id,
            client_secret_hash=secret_hash,
            client_name=client_name or client_id,
            redirect_uris=list(redirect_uris),
            grant_types=list(grant_types),
            response_types=list(response_types),
            token_endpoint_auth_method=token_endpoint_auth_method,
            scope=scope,
        )
        self.db.add(client)
        await self.db.commit()
        return client, raw_secret

    async def register(
        self,
        request: ClientRegistrationRequest,
        allowed_hosts: frozenset[str],
    ) -> ClientRegistrationResponse:
        """One failed `redirect_uris` entry refuses the whole request rather
        than silently dropping it — a client that believes it registered a
        URI that was actually dropped would fail confusingly, much later, at
        authorize time."""
        client, raw_secret = await self.create(
            redirect_uris=request.redirect_uris,
            client_name=request.client_name,
            token_endpoint_auth_method=request.token_endpoint_auth_method,
            scope=request.scope,
            grant_types=request.grant_types,
            response_types=request.response_types,
            allowed_hosts=allowed_hosts,
        )

        return ClientRegistrationResponse(
            client_id=client.client_id,
            client_secret=raw_secret,
            client_id_issued_at=int(client.created_at.replace(tzinfo=UTC).timestamp()),
            redirect_uris=client.redirect_uris,
            client_name=client.client_name,
            grant_types=client.grant_types,
            response_types=client.response_types,
            token_endpoint_auth_method=client.token_endpoint_auth_method,
            scope=client.scope,
        )

    async def get(self, client_id: str) -> OAuthClient | None:
        return await OAuthClient.get_by_client_id(self.db, client_id)

    @staticmethod
    def verify_secret(client: OAuthClient, presented_secret: str | None) -> bool:
        """Whether `presented_secret` authenticates as `client`.

        A public client (no stored hash) needs none — PKCE binds its exchange
        instead — so this is True for one regardless of what was presented.
        """
        if client.client_secret_hash is None:
            return True
        if presented_secret is None:
            return False
        return ApiKeyToken.matches(presented_secret, client.client_secret_hash)
