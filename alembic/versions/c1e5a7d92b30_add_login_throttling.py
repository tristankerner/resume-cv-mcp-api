"""add login throttling

Counters for the fail2ban-style throttle on password logins: five columns on
`users` for the per-account ladder, and `auth_failures` for the per-address
tally that catches spraying across many usernames.

Additive only, and every new column is either nullable or carries a server
default, so a database migrated ahead of the deploy stays readable by the code
already running in front of it.

Revision ID: c1e5a7d92b30
Revises: d3f2b8c91a04
Create Date: 2026-08-25 11:10:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1e5a7d92b30"
down_revision: Union[str, Sequence[str], None] = "d3f2b8c91a04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default rather than a plain default: existing rows are backfilled
    # by the DDL itself, so the NOT NULL holds without a second UPDATE pass.
    op.add_column(
        "users",
        sa.Column(
            "failed_login_count", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "users", sa.Column("first_failed_login_at", sa.DateTime(), nullable=True)
    )
    op.add_column("users", sa.Column("locked_until", sa.DateTime(), nullable=True))
    op.add_column(
        "users",
        sa.Column("lock_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "users", sa.Column("locked_permanently_at", sa.DateTime(), nullable=True)
    )

    op.create_table(
        "auth_failures",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("address", sa.String(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_failure_at", sa.DateTime(), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(), nullable=False),
        sa.Column("banned_until", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("address"),
    )
    op.create_index(
        op.f("ix_auth_failures_address"), "auth_failures", ["address"], unique=True
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_auth_failures_address"), table_name="auth_failures")
    op.drop_table("auth_failures")
    op.drop_column("users", "locked_permanently_at")
    op.drop_column("users", "lock_count")
    op.drop_column("users", "locked_until")
    op.drop_column("users", "first_failed_login_at")
    op.drop_column("users", "failed_login_count")
