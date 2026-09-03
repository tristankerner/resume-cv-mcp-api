"""Seeds the three example documents into a brand-new account, so a fresh
user is never empty. Applies to every new user, including the bootstrap
admin — see UserService.register_user and AdminBootstrapper.ensure_admin,
the two call sites, both in the same transaction as the user insert.

Documents are seeded from the `document_schema` catalogue's latest row per
type, not from `examples/*.json` on disk. Those files still exist and still
matter — they are what `examples/seed-documents.sh` and
`tests/test_path_resolution.py` use, and they are what the frozen
`alembic/versions/schema_snapshots/` files were generated from — but they
stopped being what a new account is seeded from once the catalogue existed
as a single, versioned source of truth for "what a new account starts with".
Editing an example file no longer changes what anyone receives; only a new
`document_schema` row (i.e. a migration) does. `tests/test_catalogue_examples.py`
keeps the two from drifting apart.
"""

from typing import ClassVar

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.document import Document
from persistence.document_schema import DocumentSchema
from services.document.document_types import DocumentType, DocumentTypeRegistry


class DocumentSeeder:
    REVISION_NOTE: ClassVar[str] = "Example content created with the account"

    async def seed(self, db: AsyncSession, owner_id: int) -> None:
        """Write one private revision of each document type for `owner_id`,
        from the latest `document_schema` row for that type. Does not
        commit — the caller owns the transaction, and `owner_id` must
        already be flushed: `Document.created_by` is a foreign key to
        `users.id` and part of its composite primary key.

        Rows are added directly rather than through
        `Document.upsert_document`, which commits per document: going through
        it would commit the half-seeded account three times over and break
        the one guarantee both call sites are written around — that a user
        and their documents arrive together or not at all. Nothing here needs
        what upsert offers anyway, since a brand-new owner has no revision to
        compare against: every document is revision 1, private, by
        construction.

        A malformed or missing catalogue row raises here, which means a
        registration fails loudly rather than shipping an incomplete
        account — the cost of no longer validating examples at import time,
        as `_ENTRIES` used to. `tests/test_catalogue_examples.py` is the
        other half of that guard: it catches a bad example at test time,
        before it ever reaches a real registration.
        """
        catalogue = {
            row.document_type: row for row in await DocumentSchema.latest_per_type(db)
        }
        for document_type in DocumentType:
            row = catalogue.get(document_type.value)
            if row is None:
                raise RuntimeError(
                    f"No document_schema row for type {document_type.value!r}. "
                    "Migrations did not run, or this build's DocumentType has "
                    "no matching catalogue entry."
                )

            model = DocumentTypeRegistry.MODELS_BY_TYPE[document_type]
            try:
                validated = model.model_validate(row.example)
            except ValidationError as error:
                raise RuntimeError(
                    f"document_schema example for type {document_type.value!r} "
                    f"version {row.version} does not validate against "
                    f"{model.__name__}: {error}"
                ) from error

            db.add(
                Document(
                    created_by=owner_id,
                    name=document_type.value,
                    revision_id=1,
                    type=document_type.value,
                    public=False,
                    revision_note=self.REVISION_NOTE,
                    data=validated.model_dump(by_alias=True),
                    schema_version=row.version,
                )
            )
