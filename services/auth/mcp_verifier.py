from fastmcp.server.auth import AccessToken, TokenVerifier

from services.auth.auth_service import AuthService
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService


class McpTokenVerifier(TokenVerifier):
    """Adapts the API's own credentials to FastMCP's token interface.

    Verification is delegated to AuthService.authenticate so MCP and HTTP agree
    by construction on what a credential means. Scopes are resolved from the
    user record on every call, which costs a lookup but means a revoked role
    stops working immediately rather than when the token expires.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        config_service = ConfigService.get_without_deps()

        async with DatabaseService.session() as db:
            principal = await AuthService(db, token, config_service).authenticate()

        if principal is None:
            return None

        scopes = sorted(str(scope) for scope in principal.scopes)
        return AccessToken(
            token=token,
            client_id=principal.username,
            subject=principal.username,
            scopes=scopes,
            expires_at=principal.expires_at,
            claims={
                "sub": principal.username,
                "user_id": principal.user_id,
                "scopes": scopes,
            },
        )
