from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from services.document.document_names import DocumentNamePolicy


class UpsertStatus(StrEnum):
    """What a write did, as the API reports it.

    Defined here rather than beside `Document.upsert_document`, which answers
    the same question as a bool: this is the wire contract, and a value
    renamed in persistence should not be able to change what a client reads
    without anyone touching the response model.
    """

    CREATED = "created"
    UNCHANGED = "unchanged"


class CreateDocumentRequest[T](BaseModel):
    name: str = Field(max_length=255)
    revision_note: str
    # Three-valued on purpose. Omitting it leaves the flag as the current
    # revision has it, so an edit that only changes content cannot silently
    # unpublish a document — the failure mode of a plain `bool = False`. A
    # document with no current revision defaults closed.
    public: bool | None = None
    data: T

    _validate_name = field_validator("name")(DocumentNamePolicy.validate)


class CreateDocumentResponse(BaseModel):
    name: str
    revision_id: int
    # Whether this write produced a new revision or matched the current one
    # exactly (both `data` and `public` unchanged) and wrote nothing —
    # see Document.upsert_document. A no-op write records no revision_note,
    # since there is no new revision to attach it to.
    status: UpsertStatus
