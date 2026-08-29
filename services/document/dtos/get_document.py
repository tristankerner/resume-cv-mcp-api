from datetime import datetime

from pydantic import BaseModel


class GetDocumentResponse[T](BaseModel):
    """Envelope around one document revision."""

    name: str
    revision_id: int
    revision_note: str | None = None
    type: str
    public: bool
    created_at: datetime
    data: T


class GetDocumentRevisionsResponse[T](BaseModel):
    """The envelope every private read returns, always — one element by
    default, more when `revisions` asks for it. A document with one revision
    returns a one-element list; the document not existing at all is a 404,
    not an empty list."""

    data: list[GetDocumentResponse[T]]
