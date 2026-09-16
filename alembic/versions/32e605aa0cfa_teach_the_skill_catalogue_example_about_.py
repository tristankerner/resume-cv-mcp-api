"""teach the skill catalogue example about multi contact events and the removed attachment tool

Rewrites the `document_schema` row `(version=3, document_type='skill')`'s
`example` so its `offer-application-record` step stops naming `attachments`
as something to assemble and commit over MCP - see `a806bc88a451` §6, which
removed the MCP attachment tools. The step now tells the model to report the
documents it produced and say they are uploaded through the web client
instead.

`json_schema` is untouched - `ResumeSkill` itself has not changed shape, only
this example's content. `data.schema_version` moves to `2.5.0` independently
of that for the same reason `a17c4e9b2d50` moved it to `2.4.0`: it stamps the
example content, not the model.

Frozen snapshots, same reasoning as `a17c4e9b2d50`: read from
`schema_snapshots/` at migration run time rather than from `examples/` at the
repository root, so this migration's behaviour cannot change if that file is
edited again later.

Revision ID: 32e605aa0cfa
Revises: a806bc88a451
Create Date: 2026-09-15 21:21:44.955560

"""

import json
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "32e605aa0cfa"
down_revision: Union[str, Sequence[str], None] = "a806bc88a451"
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
        .where(table.c.version == 3, table.c.document_type == "skill")
        .values(example=_load("v3_2_skill.example.json"))
    )


def downgrade() -> None:
    """Downgrade schema."""
    table = _document_schema_table()
    op.execute(
        table.update()
        .where(table.c.version == 3, table.c.document_type == "skill")
        .values(example=_load("v3_1_skill.example.json"))
    )
