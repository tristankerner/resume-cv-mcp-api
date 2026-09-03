"""add document schema table

A catalogue of what each document type's payload looked like at each version:
`document_schema(version, document_type) -> (description, json_schema, example)`.
Keyed on the pair rather than a surrogate id so "starts at schema 1" is
literally true for every type — see `SCHEMA_VERSIONING_PLAN.md` decision 2.

This revision only creates the table and seeds the three v1 rows. Nothing
reads `documents.type` here and nothing writes `documents.schema_version` yet
— that column does not exist until the next revision. The two are split so
each has its own, independently reviewable upgrade/downgrade.

`json_schema` and `example` are frozen JSON files under
`schema_snapshots/`, generated once, offline, from the v1 models recovered at
`git show main:services/document/dtos/resume_object.py` (`main` is the last
pre-v2 commit) and from `git show main:examples/*.json`. They are a
historical record of what schema version 1 was, not a copy of what the
current models produce — nothing regenerates them and nothing in `services/`
may import them. Frozen rather than inline: the six rows this catalogue ends
up holding (three here, three in the next-but-one revision) run to roughly
100 KB of schema-plus-example, which would make this file unreadable if
pasted in directly.

Revision ID: cb6cd1ae433b
Revises: f3106ced7318
Create Date: 2026-09-03 08:56:25.795071

"""
import json
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "cb6cd1ae433b"
down_revision: Union[str, Sequence[str], None] = "f3106ced7318"
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
    op.create_table(
        "document_schema",
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("document_type", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("json_schema", sa.JSON(), nullable=False),
        sa.Column("example", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("version", "document_type"),
    )

    op.bulk_insert(
        _document_schema_table(),
        [
            {
                "version": 1,
                "document_type": "resume",
                "description": (
                    "The original, bespoke resume shape: profile, contact, "
                    "skill_groups, jobs, personal_projects. Superseded by "
                    "version 2, the JSON Resume 1.0 structure."
                ),
                "json_schema": _load("v1_resume.schema.json"),
                "example": _load("v1_resume.example.json"),
            },
            {
                "version": 1,
                "document_type": "metadata",
                "description": (
                    "ResumeMetadata as of the v1 resume shape. The model is "
                    "unchanged in version 2 — verified byte-identical "
                    "model_json_schema() output — but its content still names "
                    "v1 paths (resume.jobs[].highlights[].summary), which "
                    "version 2's content rewrites to the v2 paths "
                    "(resume.work[].highlights[].summary)."
                ),
                "json_schema": _load("v1_metadata.schema.json"),
                "example": _load("v1_metadata.example.json"),
            },
            {
                "version": 1,
                "document_type": "skill",
                "description": (
                    "ResumeSkill as of the v1 resume shape. Like metadata, "
                    "the model is unchanged in version 2 and only its path "
                    "content differs."
                ),
                "json_schema": _load("v1_skill.schema.json"),
                "example": _load("v1_skill.example.json"),
            },
        ],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("document_schema")
