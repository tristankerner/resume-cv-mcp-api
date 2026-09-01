from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from services.document.document_service import DocumentService
from services.document.dtos.create_document import (
    CreateDocumentRequest,
    CreateDocumentResponse,
)
from services.document.dtos.delete_document import DeleteDocumentResponse
from services.document.dtos.get_document import (
    GetDocumentResponse,
    GetDocumentRevisionsResponse,
)
from services.document.dtos.get_schemas import GetSchemasResponse
from services.document.dtos.list_documents import ListDocumentsResponse
from services.document.dtos.rename_document import (
    RenameDocumentRequest,
    RenameDocumentResponse,
)
from services.document.dtos.resume_object import Resume, ResumeMetadata, ResumePrivate
from services.document.dtos.resume_skill import ResumeSkill


class DocumentsRouter:
    # `revisions=3&order=oldest_first` means the three MOST RECENT revisions,
    # oldest of those three first — not the three oldest revisions overall.
    # `order` governs only how the already-selected slice is arranged.
    REVISIONS = Annotated[int, Query(ge=1, le=50)]
    ORDER = Literal["newest_first", "oldest_first"]

    DocumentServiceDep = Annotated[
        DocumentService, Depends(DocumentService.get_with_deps)
    ]

    def __init__(self) -> None:
        self.router = APIRouter()
        self._register()

    def _register(self) -> None:
        self.router.get("/public/{username}/resume/{document_name}")(
            self.read_public_document_by_username
        )
        self.router.get("/public/users/{user_id}/resume/{document_name}")(
            self.read_public_document_by_id
        )
        self.router.get("/documents")(self.list_documents)
        self.router.get("/documents/schemas")(self.get_document_schemas)
        self.router.get("/documents/resume/{document_name}")(self.read_document)
        self.router.post("/documents/resume")(self.upsert_document)
        self.router.get("/documents/metadata/{document_name}")(
            self.read_metadata_document
        )
        self.router.post("/documents/metadata")(self.upsert_metadata_document)
        self.router.get("/documents/skill/{document_name}")(self.read_skill_document)
        self.router.post("/documents/skill")(self.upsert_skill_document)
        # Registered after the typed routes above to make the overlap with
        # /documents/resume explicit. There is no DELETE handler on those, so
        # FastAPI resolves this regardless of order.
        self.router.delete("/documents/{document_name}")(self.delete_document)
        self.router.patch("/documents/{document_name}")(self.rename_document)

    # One route per disclosure tier, with parameterized response models: the
    # public route cannot return private fields even if the service hands it
    # the wrong object.
    async def read_public_document_by_username(
        self,
        username: str,
        document_name: str,
        document_service: DocumentServiceDep,
    ) -> GetDocumentResponse[Resume]:
        return await document_service.read_public_resume_document_by_username(
            username, document_name
        )

    async def read_public_document_by_id(
        self,
        user_id: int,
        document_name: str,
        document_service: DocumentServiceDep,
    ) -> GetDocumentResponse[Resume]:
        return await document_service.read_public_resume_document_by_id(
            user_id, document_name
        )

    async def list_documents(
        self, document_service: DocumentServiceDep
    ) -> ListDocumentsResponse:
        return await document_service.list_documents()

    async def get_document_schemas(
        self, document_service: DocumentServiceDep
    ) -> GetSchemasResponse:
        """The JSON Schema for each document type the caller may read."""
        return await document_service.get_schemas()

    async def read_document(
        self,
        document_name: str,
        document_service: DocumentServiceDep,
        revisions: REVISIONS = 1,
        order: ORDER = "newest_first",
    ) -> GetDocumentRevisionsResponse[ResumePrivate]:
        return await document_service.read_private_resume_document(
            document_name, revisions, order
        )

    async def upsert_document(
        self,
        request: CreateDocumentRequest[ResumePrivate],
        document_service: DocumentServiceDep,
    ) -> CreateDocumentResponse:
        return await document_service.upsert_resume_document(request)

    # The metadata and the skill are the resume's two companions over MCP.
    # A route per payload is what validates the shape on the way in; the resume
    # route would reject either, since it validates its body as a resume.
    async def read_metadata_document(
        self,
        document_name: str,
        document_service: DocumentServiceDep,
        revisions: REVISIONS = 1,
        order: ORDER = "newest_first",
    ) -> GetDocumentRevisionsResponse[ResumeMetadata]:
        return await document_service.read_metadata_document(
            document_name, revisions, order
        )

    async def upsert_metadata_document(
        self,
        request: CreateDocumentRequest[ResumeMetadata],
        document_service: DocumentServiceDep,
    ) -> CreateDocumentResponse:
        return await document_service.upsert_metadata_document(request)

    async def read_skill_document(
        self,
        document_name: str,
        document_service: DocumentServiceDep,
        revisions: REVISIONS = 1,
        order: ORDER = "newest_first",
    ) -> GetDocumentRevisionsResponse[ResumeSkill]:
        return await document_service.read_skill_document(
            document_name, revisions, order
        )

    async def upsert_skill_document(
        self,
        request: CreateDocumentRequest[ResumeSkill],
        document_service: DocumentServiceDep,
    ) -> CreateDocumentResponse:
        return await document_service.upsert_skill_document(request)

    async def delete_document(
        self, document_name: str, document_service: DocumentServiceDep
    ) -> DeleteDocumentResponse:
        return await document_service.delete_document(document_name)

    async def rename_document(
        self,
        document_name: str,
        request: RenameDocumentRequest,
        document_service: DocumentServiceDep,
    ) -> RenameDocumentResponse:
        return await document_service.rename_document(document_name, request)


router = DocumentsRouter().router
