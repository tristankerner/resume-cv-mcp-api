from collections.abc import Awaitable, Callable
from typing import ClassVar, NamedTuple

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult, tool

from routers.mcp_documents import DocumentPatchTools
from routers.mcp_tracking import TrackingTools
from services.auth.mcp_tools import McpToolBase
from services.auth.scopes import Scopes


class ConfirmableWrite(NamedTuple):
    """The scope a pending write needs and what performs it once the token
    is redeemed."""

    scope: Scopes
    apply: Callable[[dict], Awaitable[dict]]


class ConfirmTools(McpToolBase):
    """The one tool that commits any previewed write.

    There used to be eight `confirm_*` tools, one per `preview_*`, each
    taking the same single `confirm_token` and differing only in which
    scope its decorator required. Eight near-identical entries cost a
    client ~1,800 characters of schema on every request and gave a model
    eight chances to pick the wrong one. The token already records which
    tool issued it, so the server can dispatch on that instead of asking
    the caller to name it again — and a caller naming the wrong one was
    never information, only an error to report.

    Collapsing them moves the scope check off the decorator, which is the
    one thing here that needs care. `require_scopes` is evaluated before
    the handler runs and cannot see a token it has not read yet, so this
    tool admits any credential holding one of the write scopes and then
    enforces the specific one inside `ConfirmationService.redeem`, under
    the same row lock and strictly before the token is consumed. A
    credential holding `companies:write` alone cannot commit a previewed
    application, and finding that out does not burn its token.

    See routers/mcp.py's docstring for why the `@tool` adapter at the foot
    of this file is free-standing.
    """

    REGISTRY: ClassVar[dict[str, ConfirmableWrite]] = {
        "create_company": ConfirmableWrite(
            Scopes.COMPANIES_WRITE, TrackingTools.apply_create_company
        ),
        "add_company_stack_items": ConfirmableWrite(
            Scopes.COMPANIES_WRITE, TrackingTools.apply_add_company_stack_items
        ),
        "create_company_relationship": ConfirmableWrite(
            Scopes.COMPANIES_WRITE, TrackingTools.apply_create_company_relationship
        ),
        "create_contact": ConfirmableWrite(
            Scopes.CONTACTS_WRITE, TrackingTools.apply_create_contact
        ),
        "record_application": ConfirmableWrite(
            Scopes.APPLICATIONS_WRITE, TrackingTools.apply_record_application
        ),
        "add_application_event": ConfirmableWrite(
            Scopes.APPLICATIONS_WRITE, TrackingTools.apply_add_application_event
        ),
        "add_attachment": ConfirmableWrite(
            Scopes.APPLICATIONS_WRITE, TrackingTools.apply_add_attachment
        ),
        "resume_patch": ConfirmableWrite(
            Scopes.RESUME_WRITE, DocumentPatchTools.apply_resume_patch
        ),
    }

    WRITE_SCOPES: ClassVar[frozenset[Scopes]] = frozenset(
        entry.scope for entry in REGISTRY.values()
    )

    @classmethod
    def _authorize(cls, tool_name: str) -> None:
        entry = cls.REGISTRY.get(tool_name)
        if entry is None:
            # A pending write this build cannot perform — issued by a newer
            # one, or by a tool since retired. Same reasoning as
            # `DocumentTypeRegistry.scopes_for_stored`: refuse rather than
            # guess at what it was for.
            raise ToolError(
                f"This build does not know how to commit a {tool_name!r} write."
            )
        if entry.scope not in cls.current_scopes():
            raise ToolError(f"Requires scope: {entry.scope.value}")

    @classmethod
    async def confirm(cls, confirm_token: str) -> dict:
        redeemed = await cls.redeem_confirmation(confirm_token, cls._authorize)
        result = await cls.REGISTRY[redeemed.tool_name].apply(redeemed.payload)
        return {"confirmed": redeemed.tool_name, "result": result}


# Free-standing by necessity, not by choice — see ResumeTools' docstring in
# routers/mcp.py, which this module follows the same shape as.
@tool(
    auth=ConfirmTools.require_any_scope(*ConfirmTools.WRITE_SCOPES),
    output_schema=None,
)
async def confirm(confirm_token: str) -> ToolResult:
    """Commit the write a `preview_*` tool previewed, whichever one it was.

    Takes only the token that preview returned — never call this without
    having shown the user that preview and gotten an explicit yes in this
    conversation. Nothing else is read from the call: what gets written is
    the payload the server froze at preview time, so changing your mind
    means previewing again, not confirming differently.

    One token commits one write. Returns `confirmed`, naming the tool it
    belonged to, alongside that tool's own result.
    """
    return ConfirmTools.as_result(await ConfirmTools.confirm(confirm_token))
