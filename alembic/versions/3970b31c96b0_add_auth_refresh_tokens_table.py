"""add auth refresh tokens table

One table for the interactive session's refresh-token grant (§9 of
MCP_WRITEBACK_PLAN.md). No change to any existing table. Mirrors
`oauth_refresh_tokens` (see c926171a328a) but keyed on `session_id` instead of
an OAuth `grant_id`, since there is no client to scope this to, and it carries
`user_agent` and `last_used_at` instead of `client_id`, `scopes` and
`resource`.

Revision ID: 3970b31c96b0
Revises: cecabf408169
Create Date: 2026-09-09 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3970b31c96b0"
down_revision: Union[str, Sequence[str], None] = "cecabf408169"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "auth_refresh_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("rotated_to_id", sa.Integer(), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["rotated_to_id"], ["auth_refresh_tokens.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        op.f("ix_auth_refresh_tokens_token_hash"),
        "auth_refresh_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        op.f("ix_auth_refresh_tokens_user_id"), "auth_refresh_tokens", ["user_id"]
    )
    op.create_index(
        op.f("ix_auth_refresh_tokens_session_id"),
        "auth_refresh_tokens",
        ["session_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_auth_refresh_tokens_session_id"), table_name="auth_refresh_tokens"
    )
    op.drop_index(
        op.f("ix_auth_refresh_tokens_user_id"), table_name="auth_refresh_tokens"
    )
    op.drop_index(
        op.f("ix_auth_refresh_tokens_token_hash"), table_name="auth_refresh_tokens"
    )
    op.drop_table("auth_refresh_tokens")
