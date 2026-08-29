from datetime import datetime

from pydantic import BaseModel


class DocumentSummary(BaseModel):
    """A directory entry: enough to pick a document, none of its content."""

    name: str
    type: str
    public: bool
    revision_id: int
    revision_note: str | None = None
    created_at: datetime


class ListDocumentsResponse(BaseModel):
    data: list[DocumentSummary]
