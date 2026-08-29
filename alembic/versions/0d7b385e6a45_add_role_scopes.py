"""add role_scopes

A role, until now, was a Python-side dict (`ROLE_SCOPES` in
services/auth/scopes.py). This moves that mapping into the database, seeded
with what the dict already granted plus the new `resume:delete` scope —
withheld from `mcp` on purpose, since that is the exact case this feature
exists for: a human can delete their own document, the MCP credential acting
for them cannot.

Seeded with `op.bulk_insert` against a lightweight table definition rather than
the ORM model, so this migration keeps working after the model changes.

Revision ID: 0d7b385e6a45
Revises: c1e5a7d92b30
Create Date: 2026-08-25 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0d7b385e6a45"
down_revision: Union[str, Sequence[str], None] = "c1e5a7d92b30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SEED = {
    "admin": [
        "resume:read:public",
        "resume:read:private",
        "resume:write",
        "resume:delete",
        "users:admin",
    ],
    "mcp": [
        "resume:read:public",
        "resume:read:private",
    ],
}


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "role_scopes",
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("role", "scope"),
    )

    role_scopes_table = sa.table(
        "role_scopes",
        sa.column("role", sa.String()),
        sa.column("scope", sa.String()),
    )
    op.bulk_insert(
        role_scopes_table,
        [
            {"role": role, "scope": scope}
            for role, scopes in SEED.items()
            for scope in scopes
        ],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("role_scopes")
