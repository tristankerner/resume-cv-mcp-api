"""update skill catalogue example for the unified confirm tool

Rewrites the `document_schema` row `(version=3, document_type='skill')`'s
`example` so its write-back step names `confirm` rather than
`confirm_resume_patch`. The eight per-tool `confirm_*` tools were collapsed
into one `confirm` that dispatches on what the token was issued for, so the
seeded example was instructing a new account's model to call a tool that no
longer exists.

`json_schema` is untouched - `ResumeSkill` itself has not changed shape, only
this example's content. `data.schema_version` moves to `2.4.0` independently
of that for the same reason: it stamps the example content, not the model.

Frozen snapshots, same reasoning as `eb599ee50641`: read from
`schema_snapshots/` at migration run time rather than from `examples/` at the
repository root, so this migration's behaviour cannot change if that file is
edited again later.

Revision ID: a17c4e9b2d50
Revises: bec5c0b76281
Create Date: 2026-09-11 12:04:00.000000

"""
import json
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a17c4e9b2d50'
down_revision: Union[str, Sequence[str], None] = 'bec5c0b76281'
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
        .values(example=_load("v3_1_skill.example.json"))
    )


def downgrade() -> None:
    """Downgrade schema."""
    table = _document_schema_table()
    op.execute(
        table.update()
        .where(table.c.version == 3, table.c.document_type == "skill")
        .values(example=_load("v3_skill.example.json"))
    )
