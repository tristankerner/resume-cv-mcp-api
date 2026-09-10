"""add v3 document schema rows

Inserts the three version-3 `document_schema` rows: the widened JSON Resume
shape for `resume` (`fineTuningData.logistics.travel`,
`projects[].highlights[]` as objects — see MCP_WRITEBACK_PLAN.md §5), and —
for `metadata` and `skill` — a schema that is byte-identical to their
version-2 row but a materially different `example`, the same asymmetry
`bcc6cfe85c34` records for v1 -> v2: `ResumeMetadata` and `ResumeSkill` did
not change shape, only the document *content* did (the new project-highlight
field guides, the write-back procedure steps, the confirmation-gate
guardrails).

`json_schema` and `example` are frozen snapshots, same reasoning as
`bcc6cfe85c34`: generated offline from the current models and examples, not
imported from `services/` at migration time, so this migration's behaviour
cannot silently change if those models change later.

Revision ID: 6ec9cebeb712
Revises: 9fd383ecfc98
Create Date: 2026-09-09 00:10:00.000000

"""
import json
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6ec9cebeb712"
down_revision: Union[str, Sequence[str], None] = "9fd383ecfc98"
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
                "version": 3,
                "document_type": "resume",
                "description": (
                    "JSON Resume 1.0, extended with fineTuningData.logistics"
                    ".travel and with projects[].highlights[] widened from "
                    "plain strings to the same Highlight object work[] and "
                    "volunteer[] already use — a project highlight now "
                    "carries an id, specifics, tech, metrics and story, and "
                    "is eligible to lead a role family's summary. See "
                    "ResumePrivate and MCP_WRITEBACK_PLAN.md §5."
                ),
                "json_schema": _load("v3_resume.schema.json"),
                "example": _load("v3_resume.example.json"),
            },
            {
                "version": 3,
                "document_type": "metadata",
                "description": (
                    "ResumeMetadata. The model is unchanged from version 2 — "
                    "verified byte-identical model_json_schema() output — so "
                    "this row exists only because the *content* changed: "
                    "field guides for resume.projects[].highlights[], a "
                    "field guide for resume.fineTuningData.logistics.travel, "
                    "and disclosure.never_publish entries for the new "
                    "project-highlight private fields."
                ),
                "json_schema": _load("v3_metadata.schema.json"),
                "example": _load("v3_metadata.example.json"),
            },
            {
                "version": 3,
                "document_type": "skill",
                "description": (
                    "ResumeSkill. Like metadata, the model is unchanged from "
                    "version 2 and this row exists for its rewritten "
                    "content: the record-application step is replaced by "
                    "offer-application-record and a new offer-resume-updates "
                    "step is added, both gated by the new preview/confirm "
                    "MCP write tools and their guardrails."
                ),
                "json_schema": _load("v3_skill.schema.json"),
                "example": _load("v3_skill.example.json"),
            },
        ],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM document_schema WHERE version = 3")
