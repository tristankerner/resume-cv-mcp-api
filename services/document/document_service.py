from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.document import Document, DocumentNameConflict, DocumentTypeConflict
from persistence.user import User
from services.auth.auth_service import AuthService
from services.auth.exceptions import AuthErrors
from services.auth.principal import Principal
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService
from services.document.document_reader import DocumentReader
from services.document.document_types import DocumentType, DocumentTypeRegistry
from services.document.dtos.create_document import (
    CreateDocumentRequest,
    CreateDocumentResponse,
    UpsertStatus,
)
from services.document.dtos.delete_document import DeleteDocumentResponse
from services.document.dtos.get_document import (
    GetDocumentResponse,
    GetDocumentRevisionsResponse,
)
from services.document.dtos.get_schemas import GetSchemasResponse
from services.document.dtos.list_documents import DocumentSummary, ListDocumentsResponse
from services.document.dtos.rename_document import (
    RenameDocumentRequest,
    RenameDocumentResponse,
)
from services.document.dtos.resume_object import (
    PublicProjection,
    ResumeMetadata,
    ResumePrivate,
    ResumePublic,
)
from services.document.dtos.resume_skill import ResumeSkill
from services.document.exceptions import DocumentErrors
from services.service_interface import ServiceProviderInterface

RevisionOrder = Literal["newest_first", "oldest_first"]


class DocumentService(ServiceProviderInterface):
    def __init__(self, db: AsyncSession, principal: Principal | None):
        self.db: AsyncSession = db
        self.principal: Principal | None = principal

    def _require(self, scope: Scopes) -> None:
        """401 when there is no credential at all, 403 when it isn't enough."""
        if self.principal is None:
            raise AuthErrors.credentials()
        self.principal.require_scope(scope)

    def _owner(self) -> int:
        """The caller's own id, which every private read, write and delete
        filters on. A document owned by someone else is a 404, not a 403."""
        if self.principal is None:
            raise AuthErrors.credentials()
        return self.principal.user_id

    def _reader(self, owner_id: int) -> DocumentReader:
        """Reads scoped to one owner. Shared with the MCP tools — see
        DocumentReader."""
        return DocumentReader(self.db, owner_id)

    def _envelope[T: BaseModel](
        self, doc: Document, build: Callable[[dict], T]
    ) -> GetDocumentResponse[T]:
        return GetDocumentResponse[T](
            name=doc.name,
            revision_id=doc.revision_id,
            revision_note=doc.revision_note,
            type=doc.type,
            public=doc.public,
            created_at=doc.created_at,
            data=build(doc.data),
        )

    async def _read_public(
        self, user: User | None, document_name: str
    ) -> GetDocumentResponse[ResumePublic]:
        """No credential required, by design. Never 403: whether a private
        document exists is not itself public information."""
        if user is None or not user.active:
            raise DocumentErrors.not_found()
        document = await self._reader(user.id).latest(document_name)
        if (
            document is None
            or not document.public
            or document.type != DocumentType.RESUME
        ):
            raise DocumentErrors.not_found()
        return self._envelope(
            document,
            lambda data: PublicProjection.of(ResumePrivate.model_validate(data)),
        )

    async def read_public_resume_document_by_username(
        self, username: str, document_name: str
    ) -> GetDocumentResponse[ResumePublic]:
        user = await User.get_user_by_username(self.db, username)
        return await self._read_public(user, document_name)

    async def read_public_resume_document_by_id(
        self, user_id: int, document_name: str
    ) -> GetDocumentResponse[ResumePublic]:
        user = await User.get_user_by_id(self.db, user_id)
        return await self._read_public(user, document_name)

    # The resume and its two companions differ only in the model their payload
    # validates against, so the scope check, type check and load are shared.
    # Each envelope is built by its own method to keep the type parameter
    # concrete, which is what lets FastAPI document the three payloads
    # separately.
    async def _read_private(
        self,
        document_name: str,
        expected: DocumentType,
        revisions: int,
        order: RevisionOrder,
    ) -> list[Document]:
        self._require(DocumentTypeRegistry.SCOPES_BY_TYPE[expected].read)
        owner_id = self._owner()
        docs = await self._reader(owner_id).revisions(
            document_name, limit=revisions, newest_first=(order == "newest_first")
        )
        # Type is immutable per document, so the first revision speaks for all
        # of them. 404 rather than 400: under this route, that name is not a
        # document of this type.
        if not docs or docs[0].type != expected:
            raise DocumentErrors.not_found()
        return docs

    def _revisions_response[T: BaseModel](
        self, docs: list[Document], model: type[T]
    ) -> GetDocumentRevisionsResponse[T]:
        return GetDocumentRevisionsResponse[T](
            data=[self._envelope(doc, model.model_validate) for doc in docs]
        )

    async def _upsert[T: BaseModel](
        self, request: CreateDocumentRequest[T], doc_type: DocumentType
    ) -> CreateDocumentResponse:
        self._require(DocumentTypeRegistry.SCOPES_BY_TYPE[doc_type].write)
        owner_id = self._owner()

        document = Document(
            created_by=owner_id,
            name=request.name,
            type=doc_type.value,
            revision_note=request.revision_note,
            data=request.data.model_dump(),
        )
        # `public` is handed over separately rather than set on the instance:
        # the request may leave it unstated, and only the store can see the
        # current revision it should then be inherited from.
        try:
            result = await Document.upsert_document(
                self.db, document, public=request.public
            )
        except DocumentTypeConflict as conflict:
            raise DocumentErrors.type_conflict(
                conflict.existing_type, conflict.attempted_type
            ) from conflict
        if result.document is None:
            raise DocumentErrors.unknown_upsert_failure()

        return CreateDocumentResponse(
            name=result.document.name,
            revision_id=result.document.revision_id,
            status=UpsertStatus.CREATED if result.created else UpsertStatus.UNCHANGED,
        )

    async def read_private_resume_document(
        self, document_name: str, revisions: int, order: RevisionOrder
    ) -> GetDocumentRevisionsResponse[ResumePrivate]:
        docs = await self._read_private(
            document_name, DocumentType.RESUME, revisions, order
        )
        return self._revisions_response(docs, ResumePrivate)

    async def upsert_resume_document(
        self, request: CreateDocumentRequest[ResumePrivate]
    ) -> CreateDocumentResponse:
        return await self._upsert(request, DocumentType.RESUME)

    async def read_metadata_document(
        self, document_name: str, revisions: int, order: RevisionOrder
    ) -> GetDocumentRevisionsResponse[ResumeMetadata]:
        docs = await self._read_private(
            document_name, DocumentType.METADATA, revisions, order
        )
        return self._revisions_response(docs, ResumeMetadata)

    async def upsert_metadata_document(
        self, request: CreateDocumentRequest[ResumeMetadata]
    ) -> CreateDocumentResponse:
        return await self._upsert(request, DocumentType.METADATA)

    async def read_skill_document(
        self, document_name: str, revisions: int, order: RevisionOrder
    ) -> GetDocumentRevisionsResponse[ResumeSkill]:
        docs = await self._read_private(
            document_name, DocumentType.SKILL, revisions, order
        )
        return self._revisions_response(docs, ResumeSkill)

    async def upsert_skill_document(
        self, request: CreateDocumentRequest[ResumeSkill]
    ) -> CreateDocumentResponse:
        return await self._upsert(request, DocumentType.SKILL)

    def _readable_types(self) -> set[DocumentType]:
        """Whichever document types the caller holds a read scope for.

        Shared by list_documents and get_schemas, which refuse only when the
        caller may read none of the three — a key narrowed to the skill
        document sees the skill type, not a 403 for the other two.
        """
        if self.principal is None:
            raise AuthErrors.credentials()
        readable = DocumentTypeRegistry.readable(self.principal.scopes)
        if not readable:
            raise AuthErrors.insufficient_any_scope(
                str(scope) for scope in DocumentTypeRegistry.READ_SCOPES
            )
        return readable

    async def list_documents(self) -> ListDocumentsResponse:
        """Names, types and revision numbers of the caller's own documents —
        no content."""
        readable = self._readable_types()
        owner_id = self._owner()
        docs = await self._reader(owner_id).list_latest()
        return ListDocumentsResponse(
            data=[
                DocumentSummary(
                    name=doc.name,
                    type=doc.type,
                    public=doc.public,
                    revision_id=doc.revision_id,
                    revision_note=doc.revision_note,
                    created_at=doc.created_at,
                )
                for doc in docs
                if doc.type in readable
            ]
        )

    async def get_schemas(self) -> GetSchemasResponse:
        """The JSON Schema for each document type the caller may read.

        Authorization mirrors list_documents. The schemas are precomputed in
        DocumentTypeRegistry.SCHEMAS_BY_TYPE: model_json_schema() is not cheap
        and these never change at runtime.
        """
        readable = self._readable_types()
        return GetSchemasResponse(
            schemas={
                doc_type.value: DocumentTypeRegistry.SCHEMAS_BY_TYPE[doc_type]
                for doc_type in readable
            }
        )

    async def delete_document(self, document_name: str) -> DeleteDocumentResponse:
        """Delete every revision of one of the caller's own documents.

        The route names only a document, so which delete scope applies is not
        known until it is loaded. The resulting 401, then 404, then 403
        ordering is deliberate: a name the caller does not own stays
        indistinguishable from one that does not exist.
        """
        owner_id = self._owner()
        document = await self._reader(owner_id).latest(document_name)
        if document is None:
            raise DocumentErrors.not_found()

        scopes = DocumentTypeRegistry.scopes_for_stored(document.type)
        if scopes is None:
            # A type this build does not know. Refusing to delete is the safe
            # direction: nothing here can say what the row is.
            raise DocumentErrors.not_found()
        self._require(scopes.delete)

        deleted = await Document.delete_all_revisions(self.db, owner_id, document_name)
        if deleted == 0:
            raise DocumentErrors.not_found()
        return DeleteDocumentResponse(name=document_name, revisions_deleted=deleted)

    async def rename_document(
        self, document_name: str, request: RenameDocumentRequest
    ) -> RenameDocumentResponse:
        """Move every revision of one of the caller's own documents to a new
        name.

        Same 401, then 404, then 403 ordering as delete_document. Requires both
        the type's write and delete scopes: a rename writes rows under the new
        name and destroys rows under the old one.
        """
        owner_id = self._owner()
        document = await self._reader(owner_id).latest(document_name)
        if document is None:
            raise DocumentErrors.not_found()

        scopes = DocumentTypeRegistry.scopes_for_stored(document.type)
        if scopes is None:
            raise DocumentErrors.not_found()
        self._require(scopes.write)
        self._require(scopes.delete)

        # Caught before the store, which would otherwise report the document
        # colliding with itself — "a document named 'x' already exists" is a
        # true but useless answer to "rename x to x".
        if request.name == document_name:
            raise DocumentErrors.rename_to_same_name()

        try:
            moved = await Document.rename(
                self.db, owner_id, document_name, request.name
            )
        except DocumentNameConflict as conflict:
            raise DocumentErrors.name_conflict(conflict.name) from conflict
        if moved == 0:
            raise DocumentErrors.not_found()

        return RenameDocumentResponse(
            old_name=document_name, name=request.name, revisions_moved=moved
        )

    @staticmethod
    def get_with_deps(
        db: Annotated[AsyncSession, Depends(DatabaseService.get_async_db_session)],
        principal: Annotated[
            Principal | None, Depends(AuthService.get_optional_principal)
        ],
    ) -> DocumentService:
        return DocumentService(db, principal)
