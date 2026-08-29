from pydantic import BaseModel, Field, field_validator

from services.document.document_names import DocumentNamePolicy


class RenameDocumentRequest(BaseModel):
    name: str = Field(max_length=255)

    _validate_name = field_validator("name")(DocumentNamePolicy.validate)


class RenameDocumentResponse(BaseModel):
    old_name: str
    name: str
    revisions_moved: int
