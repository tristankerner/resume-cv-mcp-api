"""add pending writes table

One table for the two-phase MCP write confirmation gate (§3 of
MCP_WRITEBACK_PLAN.md). No change to any existing table. Modelled on
`oauth_refresh_tokens` (see c926171a328a) but single-use (`consumed_at`
rather than `revoked_at`/`rotated_to_id`) and carrying the `tool_name`,
`payload` and `preview` a preview call was issued with.

Revision ID: 9fd383ecfc98
Revises: 3970b31c96b0
Create Date: 2026-09-09 00:05:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9fd383ecfc98"
down_revision: Union[str, Sequence[str], None] = "3970b31c96b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "pending_writes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("preview", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        op.f("ix_pending_writes_token_hash"),
        "pending_writes",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        op.f("ix_pending_writes_user_id"), "pending_writes", ["user_id"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_pending_writes_user_id"), table_name="pending_writes")
    op.drop_index(op.f("ix_pending_writes_token_hash"), table_name="pending_writes")
    op.drop_table("pending_writes")
