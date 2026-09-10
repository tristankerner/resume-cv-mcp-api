from fastapi import HTTPException
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import require_scopes
from fastmcp.tools import ToolResult, tool
from pydantic import TypeAdapter

from services.auth.mcp_tools import McpToolBase
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService
from services.document.dtos.resume_patch import PatchOp
from services.document.resume_patch_service import ResumePatchError, ResumePatchService

_PATCH_OPS_ADAPTER: TypeAdapter[list[PatchOp]] = TypeAdapter(list[PatchOp])


class DocumentPatchTools(McpToolBase):
    """The resume write-back MCP tools: describe the operation set, preview
    a patch, and confirm it. See routers/mcp.py's docstring for why the
    `@tool` adapters at the foot of this file are free-standing.

    Metadata and skill documents get no MCP write path — they describe how
    tailoring works rather than what happened, and a model editing its own
    instructions mid-run is a failure mode with no upside; `describe_resume
    _schema`'s output says so.
    """

    @classmethod
    def describe_resume_schema(cls) -> dict:
        return ResumePatchService.describe()

    @classmethod
    async def preview_resume_patch(cls, document_name: str, ops: list[dict]) -> dict:
        parsed = _PATCH_OPS_ADAPTER.validate_python(ops)
        async with DatabaseService.session() as db:
            service = ResumePatchService(db, cls.principal())
            try:
                touched = await service.preview(document_name, parsed)
            except ResumePatchError as error:
                raise ToolError(str(error)) from error
            except HTTPException as error:
                raise ToolError(str(error.detail)) from error
        preview = {
            "document_name": document_name,
            "touched": [
                {"path": item.path, "before": item.before, "after": item.after}
                for item in touched
            ],
        }
        payload = {
            "document_name": document_name,
            "ops": [op.model_dump(mode="json") for op in parsed],
        }
        return await cls.issue_preview("resume_patch", payload, preview)

    @classmethod
    async def confirm_resume_patch(cls, confirm_token: str) -> dict:
        payload = await cls.redeem_confirmation("resume_patch", confirm_token)
        parsed = _PATCH_OPS_ADAPTER.validate_python(payload["ops"])
        async with DatabaseService.session() as db:
            service = ResumePatchService(db, cls.principal())
            try:
                result = await service.apply(payload["document_name"], parsed)
            except ResumePatchError as error:
                raise ToolError(str(error)) from error
            except HTTPException as error:
                raise ToolError(str(error.detail)) from error
        return result.model_dump(mode="json")


# Free-standing by necessity, not by choice — see ResumeTools' docstring in
# routers/mcp.py, which this module follows the same shape as.
@tool(auth=require_scopes(Scopes.RESUME_READ), output_schema=None)
async def describe_resume_schema() -> ToolResult:
    """What may be changed on the resume over MCP, and how.

    Call this before proposing any resume change. It lists every write
    operation with its arguments and a worked example, the enumerated
    vocabularies (skill level, role location, engagement, location kind)
    spelled out as values, the ISO 8601 date rule, and what is not
    changeable — no renames, no deletes, no metadata or skill writes, no
    `publish` flips, no edits to an existing highlight's summary. Never
    propose a change this operation set cannot make.
    """
    return DocumentPatchTools.as_result(DocumentPatchTools.describe_resume_schema())


@tool(
    auth=require_scopes(Scopes.RESUME_READ, Scopes.RESUME_WRITE),
    output_schema=None,
)
async def preview_resume_patch(document_name: str, ops: list[dict]) -> ToolResult:
    """Preview a list of patch operations against a resume document's latest
    revision. Call `describe_resume_schema` first so every op you propose is
    one the operation set can actually express.

    Resolves every op against the current document and returns the exact
    before/after of every field it would touch, plus a `confirm_token` for
    `confirm_resume_patch`. Nothing is written until that second call.
    Refused, with no token issued, if any op cannot be resolved — an unknown
    work entry, a duplicate highlight id, a skill keyword that does not
    exist yet — or if the resulting document would not validate.

    `ops` is a list of objects, each with an `op` field naming one of:
    `add_skill_keyword`, `set_skill_keyword_level`, `add_highlight`,
    `add_specific`, `add_project`, `set_logistics`, `append_narrative`.
    """
    return DocumentPatchTools.as_result(
        await DocumentPatchTools.preview_resume_patch(document_name, ops)
    )


@tool(auth=require_scopes(Scopes.RESUME_WRITE), output_schema=None)
async def confirm_resume_patch(confirm_token: str) -> ToolResult:
    """Write the patch `preview_resume_patch` previewed. Takes only the
    token it returned — never call this without having shown the user that
    preview and gotten an explicit yes in this conversation."""
    return DocumentPatchTools.as_result(
        await DocumentPatchTools.confirm_resume_patch(confirm_token)
    )
