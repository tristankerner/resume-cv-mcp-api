"""Seeds the three example documents into a brand-new account, so a fresh
user is never empty. Applies to every new user, including the bootstrap
admin — see UserService.register_user and AdminBootstrapper.ensure_admin,
the two call sites, both in the same transaction as the user insert.
"""

import json
from pathlib import Path
from typing import ClassVar, NamedTuple

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.document import Document
from services.document.document_types import DocumentType
from services.document.dtos.resume_object import ResumeMetadata, ResumePrivate
from services.document.dtos.resume_skill import ResumeSkill


class SeedEntry(NamedTuple):
    document_type: DocumentType
    name: str
    data: BaseModel


class DocumentSeeder:
    REVISION_NOTE: ClassVar[str] = "Example content created with the account"

    # Resolved from this module's own location, never the process CWD: the
    # CLI, uvicorn and pytest all run from different places.
    EXAMPLES_DIR: ClassVar[Path] = Path(__file__).resolve().parents[2] / "examples"

    # Loaded and validated once at import, the same way
    # DocumentTypeRegistry.SCHEMAS_BY_TYPE is: reading and validating these
    # files is not free, and they never change at runtime. Written out one
    # entry at a time rather than derived from a comprehension, which would
    # need EXAMPLES_DIR from a scope of its own that cannot see this one. A
    # malformed example file is a build error, so a ValidationError here is
    # left to propagate rather than caught.
    _ENTRIES: ClassVar[tuple[SeedEntry, ...]] = (
        SeedEntry(
            document_type=DocumentType.RESUME,
            name="resume",
            data=ResumePrivate.model_validate(
                json.loads((EXAMPLES_DIR / "resume.example.json").read_text())["data"]
            ),
        ),
        SeedEntry(
            document_type=DocumentType.METADATA,
            name="metadata",
            data=ResumeMetadata.model_validate(
                json.loads((EXAMPLES_DIR / "resume.metadata.example.json").read_text())[
                    "data"
                ]
            ),
        ),
        SeedEntry(
            document_type=DocumentType.SKILL,
            name="skill",
            data=ResumeSkill.model_validate(
                json.loads((EXAMPLES_DIR / "resume.skill.example.json").read_text())[
                    "data"
                ]
            ),
        ),
    )

    async def seed(self, db: AsyncSession, owner_id: int) -> None:
        """Write one private revision of each example document for
        `owner_id`. Does not commit — the caller owns the transaction, and
        `owner_id` must already be flushed: `Document.created_by` is a
        foreign key to `users.id` and part of its composite primary key.

        Rows are added directly rather than through
        `Document.upsert_document`, which commits per document: going through
        it would commit the half-seeded account three times over and break
        the one guarantee both call sites are written around — that a user
        and their documents arrive together or not at all. Nothing here needs
        what upsert offers anyway, since a brand-new owner has no revision to
        compare against: every document is revision 1, private, by
        construction.
        """
        for entry in self._ENTRIES:
            db.add(
                Document(
                    created_by=owner_id,
                    name=entry.name,
                    revision_id=1,
                    type=entry.document_type.value,
                    public=False,
                    revision_note=self.REVISION_NOTE,
                    data=entry.data.model_dump(),
                )
            )
