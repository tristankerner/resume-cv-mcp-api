import json

from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthCheck, require_scopes
from fastmcp.server.dependencies import get_access_token
from fastmcp.tools import ToolResult, tool
from fastmcp.utilities.authorization import AuthContext
from mcp.types import TextContent
from pydantic import ValidationError

from persistence.document import Document
from services.auth.scopes import ScopeResolver, Scopes
from services.database.database_service import DatabaseService
from services.document.document_reader import DocumentReader
from services.document.document_types import DocumentType, DocumentTypeRegistry


class ResumeTools:
    """The two MCP tools, and the auth helpers they share.

    `FileSystemProvider` discovers components by scanning this module for
    top-level `Tool` objects after import, so a `@tool`-decorated method here
    would never be found. The two module-level functions below are the
    required exception to the "no free-standing functions" rule: one-line
    adapters that delegate straight to this class.

    Both adapters declare `output_schema=None` and return `ToolResult`
    directly. Left to itself, FastMCP derives an output schema from a `dict`
    return annotation and then serializes the payload twice — once as a text
    content block, once as `structuredContent` — which doubles every token
    this tool costs a client. `output_schema=None` alone does not stop this;
    a `dict` result still gets a structured mirror regardless of the schema.
    Only a `ToolResult` carrying nothing but `content` is passed through
    untouched. Dropping the structured mirror rather than the text block is
    the safer direction: every client understands a text block, and
    `structuredContent` is optional in the spec. If a client turns out to
    need structured content, the revert is to drop `output_schema=None` and
    return the bare dict again.
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
    def require_any_scope(*scopes: Scopes) -> AuthCheck:
        """Allow a call holding any one of `scopes`.

        fastmcp's own `require_scopes` is an AND across everything it is
        given, which is the wrong shape for the listing tool: it spans the
        three document types, and a credential narrowed to one of them
        should see that one.
        """
        accepted = {str(scope) for scope in scopes}

        def check(context: AuthContext) -> bool:
            return context.token is not None and bool(
                accepted & set(context.token.scopes)
            )

        return check

    @staticmethod
    def as_result(payload: dict) -> ToolResult:
        """One text block, no structured mirror — see the class docstring."""
        return ToolResult(
            content=[
                TextContent(
                    type="text", text=json.dumps(payload, separators=(",", ":"))
                )
            ]
        )

    @classmethod
    def slim(cls, document: Document) -> dict:
        """Defaults dropped, or the stored payload untouched if it no longer
        validates — a document written under an older schema must still
        retrieve. The only place in the codebase where a document is
        deliberately served without validating.

        Read time only: `Document.upsert_document` dedups a write on
        `existing.data == document.data`, so slimming what gets written
        instead of what gets read would change revision identity.
        """
        try:
            model = DocumentTypeRegistry.MODELS_BY_TYPE[DocumentType(document.type)]
        except KeyError, ValueError:
            # A type this build does not know — retired, or written by a newer
            # build. Same reasoning as `DocumentTypeRegistry.scopes_for_stored`.
            return document.data
        try:
            return model.model_validate(document.data).model_dump(exclude_defaults=True)
        except ValidationError:
            return document.data

    @classmethod
    async def list_resume_documents(cls) -> dict:
        user_id = cls.current_user_id()
        held = cls.current_scopes()
        readable = DocumentTypeRegistry.readable(held)
        async with DatabaseService.session() as db:
            documents = await DocumentReader(db, user_id).list_latest()
        return {
            "documents": [
                {
                    "document_id": document.name,
                    "type": document.type,
                    "public": document.public,
                    "revision_id": document.revision_id,
                    "revision_note": document.revision_note,
                    "created_at": document.created_at.isoformat(),
                }
                for document in documents
                if document.type in readable
            ]
        }

    @classmethod
    async def retrieve_resume_data(
        cls,
        resume_id: str,
        resume_metadata_id: str | None = None,
        resume_skill_id: str | None = None,
    ) -> dict:
        user_id = cls.current_user_id()
        held = cls.current_scopes()
        if resume_metadata_id and Scopes.METADATA_READ not in held:
            raise ToolError(f"Requires scope: {Scopes.METADATA_READ.value}")
        if resume_skill_id and Scopes.SKILL_READ not in held:
            raise ToolError(f"Requires scope: {Scopes.SKILL_READ.value}")

        async with DatabaseService.session() as db:
            reader = DocumentReader(db, user_id)
            resume = await reader.latest(resume_id)
            metadata = (
                await reader.latest(resume_metadata_id) if resume_metadata_id else None
            )
            skill = await reader.latest(resume_skill_id) if resume_skill_id else None

        if resume is None:
            raise ToolError("Resume not found")
        if resume.type != DocumentType.RESUME:
            raise ToolError(f"{resume_id!r} is not a resume document")
        if metadata is not None and metadata.type != DocumentType.METADATA:
            raise ToolError(f"{resume_metadata_id!r} is not a metadata document")
        if skill is not None and skill.type != DocumentType.SKILL:
            raise ToolError(f"{resume_skill_id!r} is not a skill document")

        # A missing companion is reported as null so the client can say what
        # it is working without, rather than failing the whole retrieval.
        return {
            "resume": cls.slim(resume),
            "resume_metadata": cls.slim(metadata) if metadata else None,
            "resume_skill": cls.slim(skill) if skill else None,
        }


# Free-standing by necessity, not by choice — see ResumeTools' docstring.
@tool(
    auth=ResumeTools.require_any_scope(*DocumentTypeRegistry.READ_SCOPES),
    output_schema=None,
)
async def list_resume_documents() -> ToolResult:
    """Lists the caller's own documents: a directory, not a read.

    Each entry is the latest revision of one document — name, type, whether
    it is public, the revision number, its note, and when it was created.
    No document payloads; use `retrieve_resume_data` for content.

    Only the types this credential may read are listed, so a document absent
    from the result may exist without being readable here.

    `document_id` is the document's name. Documents have no surrogate key:
    the identifier is (owner, name), and the owner is implied by the
    credential, so the name uniquely identifies a document to its owner.
    """
    return ResumeTools.as_result(await ResumeTools.list_resume_documents())


@tool(auth=require_scopes(Scopes.RESUME_READ), output_schema=None)
async def retrieve_resume_data(
    resume_id: str,
    resume_metadata_id: str | None = None,
    resume_skill_id: str | None = None,
) -> ToolResult:
    """Retrieves the resume, its field-level metadata, and the tailoring
    instructions, in json format. Call `list_resume_documents` first to find
    the ids to pass here.

    Read `resume_skill` first and follow it: it defines the procedure, the rules
    that govern selection and wording, and the shape of the finished document.
    `resume_metadata` explains what each resume field means and how the pieces
    join. `resume` is the content itself.

    Each companion needs its own scope — `metadata:read`, `skill:read` — and
    only when its id is actually passed. Asking for one this credential may not
    read is refused rather than answered with null: null means "not stored",
    and a client told that would go on to work without a document that exists.
    """
    return ResumeTools.as_result(
        await ResumeTools.retrieve_resume_data(
            resume_id, resume_metadata_id, resume_skill_id
        )
    )
