"""add api keys table

Long-lived credentials for machine clients, so the MCP server does not have to
exchange a password for a short-lived JWT on every use. Only the hash of the
secret is stored; `prefix` is the non-secret half of the presented key and
carries the unique index that makes verification a single lookup.

Revision ID: d3f2b8c91a04
Revises: f74c464133e8
Create Date: 2026-08-24 15:05:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3f2b8c91a04"
down_revision: Union[str, Sequence[str], None] = "f74c464133e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("prefix", sa.String(), nullable=False),
        sa.Column("key_hash", sa.String(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("prefix"),
    )
    op.create_index(op.f("ix_api_keys_prefix"), "api_keys", ["prefix"], unique=True)
    op.create_index(op.f("ix_api_keys_user_id"), "api_keys", ["user_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_api_keys_user_id"), table_name="api_keys")
    op.drop_index(op.f("ix_api_keys_prefix"), table_name="api_keys")
    op.drop_table("api_keys")
