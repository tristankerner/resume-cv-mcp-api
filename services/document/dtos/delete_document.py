from pydantic import BaseModel


class DeleteDocumentResponse(BaseModel):
    name: str
    revisions_deleted: int
