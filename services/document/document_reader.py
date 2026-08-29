from sqlalchemy.ext.asyncio import AsyncSession

from persistence.document import Document


class DocumentReader:
    """Ownership-scoped reads: the single place a document is loaded for a
    known owner.

    Both surfaces go through it — `DocumentService` for HTTP and
    `ResumeTools` for MCP — so neither can drift on what "this caller's
    document" means. That mattered: the two used to call
    `Document.get_latest` independently, each passing an owner id it had
    derived its own way, and the whole ownership model rests on every one of
    those queries being scoped to the right user.

    The owner is bound once, at construction, so no call site can forget to
    pass it. Nothing here decides *whether* a caller may read what it asks
    for — that is the scope check each surface makes before it gets this far,
    with its own vocabulary for refusing.
    """

    def __init__(self, db: AsyncSession, owner_id: int):
        self.db = db
        self.owner_id = owner_id

    async def latest(self, name: str) -> Document | None:
        return await Document.get_latest(self.db, self.owner_id, name)

    async def revisions(
        self, name: str, limit: int, newest_first: bool
    ) -> list[Document]:
        return await Document.get_revisions(
            self.db, self.owner_id, name, limit=limit, newest_first=newest_first
        )

    async def list_latest(self) -> list[Document]:
        return await Document.list_latest(self.db, self.owner_id)
