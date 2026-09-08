"""update skill catalogue example for application tracking

Rewrites the `document_schema` row `(version=2, document_type='skill')`'s
`example` to document the application-tracking tools: `search_companies` /
`get_company` / `create_company` before recording, `record_application` at
the end of the run, plus the new `required_inputs`, the
`prefer-known-company-stack` tailoring rule, and the two new guardrails
against inventing a company or a contact.

`json_schema` is untouched - `ResumeSkill` itself has not changed shape, only
this example's content. `data.schema_version` moves to `2.1.0` independently
of that for the same reason: it stamps the example content, not the model.

Frozen snapshots, same reasoning as `bcc6cfe85c34`: read from
`schema_snapshots/` at migration run time rather than from `examples/` at the
repository root, so this migration's behaviour cannot change if that file is
edited again later.

Revision ID: eb599ee50641
Revises: 4b0715f7197e
Create Date: 2026-09-07 17:49:05.688499

"""
import json
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'eb599ee50641'
down_revision: Union[str, Sequence[str], None] = '4b0715f7197e'
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
        sa.column("example", sa.JSON()),
    )


def upgrade() -> None:
    """Upgrade schema."""
    table = _document_schema_table()
    op.execute(
        table.update()
        .where(table.c.version == 2, table.c.document_type == "skill")
        .values(example=_load("v2_1_skill.example.json"))
    )


def downgrade() -> None:
    """Downgrade schema."""
    table = _document_schema_table()
    op.execute(
        table.update()
        .where(table.c.version == 2, table.c.document_type == "skill")
        .values(example=_load("v2_skill.example.json"))
    )
