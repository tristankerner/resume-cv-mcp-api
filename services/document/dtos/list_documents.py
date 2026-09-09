from pydantic import BaseModel

from services.common.datetimes import UtcDatetime


class DocumentSummary(BaseModel):
    """A directory entry: enough to pick a document, none of its content."""

    name: str
    type: str
    public: bool
    revision_id: int
    revision_note: str | None = None
    created_at: UtcDatetime


class ListDocumentsResponse(BaseModel):
    data: list[DocumentSummary]
