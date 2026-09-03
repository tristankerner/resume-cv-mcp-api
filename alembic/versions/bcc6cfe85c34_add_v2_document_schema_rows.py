"""add v2 document schema rows

Inserts the three version-2 `document_schema` rows: the JSON Resume 1.0
shape for `resume`, and — for `metadata` and `skill` — a schema that is
byte-identical to their version-1 row (verified in
`tests/test_catalogue_examples.py`) but a materially different `example`.
That asymmetry is deliberate, not an oversight: `ResumeMetadata` and
`ResumeSkill` did not change shape between v1 and v2, only the path
*content* inside documents built against them did (`resume.jobs[]` ->
`resume.work[]`, and so on) — see `SCHEMA_VERSIONING_PLAN.md` decision 5. The
schema is duplicated here rather than pointed at the v1 row so that a
catalogue row is always a complete, self-contained record.

`json_schema` and `example` are frozen snapshots, same reasoning as
`cb6cd1ae433b`: generated offline from the current models and examples, not
imported from `services/` at migration time, so this migration's behaviour
cannot silently change if those models change later.

Revision ID: bcc6cfe85c34
Revises: 51d8d252662f
Create Date: 2026-09-03 08:56:36.762886

"""
import json
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bcc6cfe85c34"
down_revision: Union[str, Sequence[str], None] = "51d8d252662f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SNAPSHOTS = Path(__file__).parent / "schema_snapshots"


def _load(name: str) -> dict:
    return json.loads((SNAPSHOTS / name).read_text())


def _document_schema_table() -> sa.Table:
    return sa.table(
        "document_schema",
        sa.column("version", sa.Integer()),
        sa.column("document_type", sa.String()),
        sa.column("description", sa.String()),
        sa.column("json_schema", sa.JSON()),
        sa.column("example", sa.JSON()),
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.bulk_insert(
        _document_schema_table(),
        [
            {
                "version": 2,
                "document_type": "resume",
                "description": (
                    "JSON Resume 1.0 (https://jsonresume.org/schema), extended "
                    "with a private tailoring payload. Replaces the bespoke "
                    "version-1 shape; see ResumePrivate.MIGRATION for the "
                    "field-by-field mapping."
                ),
                "json_schema": _load("v2_resume.schema.json"),
                "example": _load("v2_resume.example.json"),
            },
            {
                "version": 2,
                "document_type": "metadata",
                "description": (
                    "ResumeMetadata. The model is unchanged from version 1 — "
                    "verified byte-identical model_json_schema() output — so "
                    "this row exists only because the *content* changed: "
                    "every path here is rewritten for the v2 resume shape "
                    "(resume.jobs[].highlights[].summary is now "
                    "resume.work[].highlights[].summary), and the document "
                    "documents v2-only capabilities the v1 example never "
                    "mentioned, such as per-specific tech."
                ),
                "json_schema": _load("v2_metadata.schema.json"),
                "example": _load("v2_metadata.example.json"),
            },
            {
                "version": 2,
                "document_type": "skill",
                "description": (
                    "ResumeSkill. Like metadata, the model is unchanged from "
                    "version 1 and this row exists for its rewritten path "
                    "content and its use of v2-only fields, e.g. "
                    "content_selection.judgement_source pointing at "
                    "specifics[].detail instead of the flattened v1 "
                    "specifics[] strings."
                ),
                "json_schema": _load("v2_skill.schema.json"),
                "example": _load("v2_skill.example.json"),
            },
        ],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM document_schema WHERE version = 2")
