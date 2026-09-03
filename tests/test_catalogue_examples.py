"""The `document_schema` catalogue must never drift from what it claims to
describe: the latest row per type must validate against that type's current
model, its `json_schema` must match that model's generated schema, and its
`example` must equal the corresponding `examples/*.json` file's `data`
key exactly — the enforcement `SCHEMA_VERSIONING_PLAN.md` decision 6 asks
for, so that changing what a new account starts with costs a new migration,
not a silent edit.
"""

import json
from pathlib import Path

from sqlalchemy import select

from persistence.document_schema import DocumentSchema
from services.database.database_service import DatabaseService
from services.document.document_types import DocumentType, DocumentTypeRegistry
from services.document.dtos.resume_object import ResumePrivate

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

FILE_BY_TYPE = {
    DocumentType.RESUME: "resume.example.json",
    DocumentType.METADATA: "resume.metadata.example.json",
    DocumentType.SKILL: "resume.skill.example.json",
}


async def _all_rows() -> list[DocumentSchema]:
    async with DatabaseService.session() as db:
        result = await db.execute(select(DocumentSchema))
        return list(result.scalars().all())


class TestCatalogueExamplesValidate:
    """Every row at the version this build currently considers current for
    its type must validate against that type's live model.

    Scoped to the current version per type — older (v1) rows will not
    validate against the current models, by design: that is not a bug in
    the catalogue, it is the historical record Phase 1 exists to freeze. See
    `SCHEMA_VERSIONING_PLAN.md` Phase 7's "hazard stated plainly" — this is
    the guard that catches a bad example baked into a migration, which is
    otherwise only discovered by running it.
    """

    async def test_every_current_row_validates(self):
        rows = await _all_rows()
        checked = set()
        for row in rows:
            document_type = DocumentType(row.document_type)
            if (
                row.version
                != DocumentTypeRegistry.CURRENT_SCHEMA_VERSION_BY_TYPE[document_type]
            ):
                continue
            model = DocumentTypeRegistry.MODELS_BY_TYPE[document_type]
            model.model_validate(row.example)  # raises on failure
            checked.add(document_type)
        assert checked == set(DocumentType)


class TestCatalogueMatchesExampleFiles:
    """Decision 6(a): the current catalogue row's `example` must equal the
    matching `examples/<type>.example.json` file's `data` key, compared
    structurally rather than as text. Editing an example therefore requires
    a new `document_schema` row — i.e. a migration — even for a typo fix.
    That strictness is the feature: it makes "what a new account receives"
    an auditable, versioned decision rather than a side effect of editing a
    JSON file.
    """

    async def test_current_row_matches_its_example_file(self):
        rows = await _all_rows()
        current = {
            DocumentType(row.document_type): row
            for row in rows
            if row.version
            == DocumentTypeRegistry.CURRENT_SCHEMA_VERSION_BY_TYPE[
                DocumentType(row.document_type)
            ]
        }
        for document_type, filename in FILE_BY_TYPE.items():
            file_data = json.loads((EXAMPLES_DIR / filename).read_text())["data"]
            assert current[document_type].example == file_data, (
                f"{filename} has drifted from the {document_type.value} "
                "catalogue's current example"
            )


class TestResumeSchemaMatchesModel:
    """Phase 3 acceptance criterion: the v2 resume row's `json_schema`
    equals `ResumePrivate.model_json_schema()` as of this commit, so a
    future model change that forgets a matching migration is caught here
    rather than discovered from a stale schema served to a client."""

    async def test_v2_resume_schema_matches_current_model(self):
        rows = await _all_rows()
        row = next(r for r in rows if r.document_type == "resume" and r.version == 2)
        assert row.json_schema == ResumePrivate.model_json_schema()
