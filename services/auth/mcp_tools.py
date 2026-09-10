import json
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthCheck
from fastmcp.server.dependencies import get_access_token
from fastmcp.tools import ToolResult
from fastmcp.utilities.authorization import AuthContext
from mcp.types import TextContent

from services.auth.principal import CredentialKind, Principal
from services.auth.scopes import ScopeResolver, Scopes
from services.confirmation.confirmation_service import ConfirmationService
from services.database.database_service import DatabaseService


class McpToolBase:
    """What every MCP tool class needs to read the calling credential and
    shape its response - shared by `ResumeTools` (routers/mcp.py) and
    `TrackingTools` (routers/mcp_tracking.py) so the two cannot drift on
    what "the caller" or "one text block" means.
    """

    @staticmethod
    def current_user_id() -> int:
        """The caller's id, from the access token's claims.

        Both HTTP and MCP resolve a credential through the same
        `AuthService.authenticate` (see mcp_verifier.py), so this only reads
        back an identity already established.
        """
        token = get_access_token()
        if token is None or "user_id" not in token.claims:
            raise ToolError("Not authenticated")
        return int(token.claims["user_id"])

    @staticmethod
    def current_scopes() -> frozenset[Scopes]:
        """What the calling credential may do, for the checks a decorator cannot make.

        `require_scopes` gates a whole tool, which is right for the scope
        every call needs and wrong for one that depends on the arguments —
        asking `retrieve_resume_data` for a metadata document should need
        `metadata:read`, and asking it for nothing but the résumé should not.
        """
        token = get_access_token()
        return ScopeResolver.parse(token.scopes) if token is not None else frozenset()

    @staticmethod
    def current_credential() -> CredentialKind:
        """How the caller proved who they are, from the access token's claims.

        `McpTokenVerifier` puts it there. A token minted by a build that did
        not falls back to API_KEY, which is what every machine credential on
        this surface was before OAuth grants could write.
        """
        token = get_access_token()
        raw = token.claims.get("credential") if token is not None else None
        try:
            return CredentialKind(raw)
        except ValueError:
            return CredentialKind.API_KEY

    @staticmethod
    def require_any_scope(*scopes: Scopes) -> AuthCheck:
        """Allow a call holding any one of `scopes`.

        fastmcp's own `require_scopes` is an AND across everything it is
        given, which is the wrong shape for a tool spanning several
        independently-scoped resources: a credential narrowed to one of them
        should still see that one.
        """
        accepted = {str(scope) for scope in scopes}

        def check(context: AuthContext) -> bool:
            return context.token is not None and bool(
                accepted & set(context.token.scopes)
            )

        return check

    @classmethod
    def principal(cls) -> Principal:
        """The calling credential, as the services expect it.

        `credential` is read from the token rather than assumed: these tools
        write, the audit log records how a change was made, and OAuth grants
        can now carry write scopes — so hardcoding a fixed kind here would
        file every connector's writes under the wrong credential kind.
        Shared by every MCP tool class that constructs a service directly
        (`TrackingTools`, `DocumentPatchTools`), so the two cannot drift on
        what "the caller" means.
        """
        return Principal(
            user_id=cls.current_user_id(),
            username="mcp",
            scopes=cls.current_scopes(),
            credential=cls.current_credential(),
        )

    @classmethod
    async def issue_preview(
        cls, tool_name: str, payload: dict[str, Any], preview: dict[str, Any]
    ) -> dict:
        """The shared shape every `preview_*` tool returns: `{"preview":
        ..., "confirm_token": ..., "expires_in": ...}`. See
        `ConfirmationService.issue` — `payload` is what the matching
        `confirm_*` call will replay, not anything the model can override
        later."""
        async with DatabaseService.session() as db:
            result = await ConfirmationService(db, cls.current_user_id()).issue(
                tool_name, payload, preview
            )
        return {
            "preview": result.preview,
            "confirm_token": result.confirm_token,
            "expires_in": result.expires_in,
        }

    @classmethod
    async def redeem_confirmation(cls, tool_name: str, confirm_token: str) -> dict:
        """The frozen payload a matching `preview_*` call issued
        `confirm_token` for, or a `ToolError` refusal — see
        `ConfirmationService.redeem`."""
        async with DatabaseService.session() as db:
            return await ConfirmationService(db, cls.current_user_id()).redeem(
                confirm_token, tool_name
            )

    @staticmethod
    def as_result(payload: dict) -> ToolResult:
        """One text block, no structured mirror.

        Left to itself, FastMCP derives an output schema from a `dict`
        return annotation and then serializes the payload twice — once as a
        text content block, once as `structuredContent` — which doubles
        every token this tool costs a client. `output_schema=None` alone
        does not stop this; a `dict` result still gets a structured mirror
        regardless of the schema. Only a `ToolResult` carrying nothing but
        `content` is passed through untouched. Dropping the structured
        mirror rather than the text block is the safer direction: every
        client understands a text block, and `structuredContent` is
        optional in the spec.
        """
        return ToolResult(
            content=[
                TextContent(
                    type="text", text=json.dumps(payload, separators=(",", ":"))
                )
            ]
        )
