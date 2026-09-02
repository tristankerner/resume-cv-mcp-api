"""Helpers shared by more than one test module.

Deliberately not in conftest.py: importing that module by name runs its
top-level environment pinning a second time, under a second temporary
directory, and the suite then migrates one database while the application
talks to another. Fixtures belong there; plain functions several modules call
belong here, where importing costs nothing.
"""

from persistence.oauth_client import OAuthClient
from services.auth.scopes import Scopes
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService
from services.oauth.tokens import TokenIssuer


class OAuthTokens:
    DEFAULT_CLIENT_ID = "test-client"

    @classmethod
    async def _ensure_client(cls, db, client_id: str) -> None:
        """Register the client the token will name, if it is not there yet.

        Added directly rather than through OAuthClientRegistry: this is about
        a row existing, not about the redirect allowlist, and going through
        the registry would tie every OAuth test to whatever
        OAUTH_ALLOWED_REDIRECT_HOSTS happens to be.
        """
        if await OAuthClient.get_by_client_id(db, client_id) is not None:
            return
        db.add(
            OAuthClient(
                client_id=client_id,
                client_secret_hash=None,
                client_name=client_id,
                redirect_uris=["https://claude.ai/callback"],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
                scope=None,
            )
        )
        await db.commit()

    @classmethod
    async def mint(
        cls, user_id: int, *scopes: Scopes, client_id: str | None = None
    ) -> str:
        """An OAuth-kind access token, minted directly rather than through the
        authorize flow — the shortcut several modules here take.

        Registers the issuing client first, because
        `AuthService._authenticate_oauth` looks it up on every request: that
        is what makes deregistration immediate, and it means a token naming a
        client that was never registered is refused exactly like one whose
        client has since been deregistered.
        """
        client_id = client_id or cls.DEFAULT_CLIENT_ID
        async with DatabaseService.session() as db:
            await cls._ensure_client(db, client_id)
            settings = ConfigService.get_with_deps().settings
            access_token, _expires_in = TokenIssuer(db, settings).mint_access_token(
                user_id=user_id, scopes=frozenset(scopes), client_id=client_id
            )
        return access_token

    @classmethod
    async def headers(cls, actor, *scopes: Scopes) -> dict[str, str]:
        return {"Authorization": f"Bearer {await cls.mint(actor.user_id, *scopes)}"}
