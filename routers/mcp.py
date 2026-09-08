from fastmcp.exceptions import ToolError
from fastmcp.server.auth import require_scopes
from fastmcp.tools import ToolResult, tool
from pydantic import ValidationError

from persistence.document import Document
from services.auth.mcp_tools import McpToolBase
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService
from services.document.document_reader import DocumentReader
from services.document.document_types import DocumentType, DocumentTypeRegistry


class ResumeTools(McpToolBase):
    """The two résumé-retrieval MCP tools.

    `FileSystemProvider` discovers components by scanning every module under
    `routers/` for top-level `Tool` objects after import, so a
    `@tool`-decorated method here would never be found. The module-level
    functions at the foot of this file are the required exception to the
    "no free-standing functions" rule: one-line adapters that delegate
    straight to this class. `routers/mcp_tracking.py` follows the same shape.

    Both adapters declare `output_schema=None` and return `ToolResult`
    directly — see `McpToolBase.as_result` for why.
    """

    @classmethod
    def slim(cls, document: Document) -> dict:
        """Defaults dropped, or the stored payload untouched if it no longer
        validates — a document written under an older schema must still
        retrieve. The only place in the codebase where a document is
        deliberately served without validating.

        `by_alias=True` is not cosmetic. The resume payload's field names are
        the JSON Resume schema's, which are camelCase, and every other surface
        serves them that way: FastAPI applies aliases to the public feed and to
        the private route, and documents are stored aliased. Dumping without it
        here would hand the MCP client `start_date` while the metadata and
        skill documents tell it to read `startDate` — instructions pointing at
        fields the client never receives, which nothing else in the stack
        detects. The metadata and skill models declare no aliases, so this is a
        no-op for them.

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
            return model.model_validate(document.data).model_dump(
                by_alias=True, exclude_defaults=True
            )
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
