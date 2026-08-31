"""add mfa credentials

Two tables for stateless multi-factor authentication: `mfa_credentials` (one
row per enrolled second factor — a TOTP secret or a backup-code set) and
`mfa_backup_codes` (the single-use codes belonging to a backup-code
credential). The MFA challenge between step one and step two of a login is a
signed JWT, not a database row, so there is no `mfa_challenges` table — see
plan.md §0.

Purely additive, so a migration applied ahead of the deploy is readable by
the code already running in front of it, the same property
c1e5a7d92b30_add_login_throttling.py's docstring calls out. `mfa_credentials`
is opt-in: a row with `activated_at IS NULL` satisfies nothing, and a user
with zero activated rows logs in exactly as before this migration existed.

Revision ID: f3106ced7318
Revises: c926171a328a
Create Date: 2026-08-31 09:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3106ced7318"
down_revision: Union[str, Sequence[str], None] = "c926171a328a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "mfa_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("secret", sa.String(), nullable=True),
        sa.Column("last_used_step", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_mfa_credentials_user_id"), "mfa_credentials", ["user_id"]
    )
    op.create_table(
        "mfa_backup_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("credential_id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["credential_id"], ["mfa_credentials.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_mfa_backup_codes_credential_id"),
        "mfa_backup_codes",
        ["credential_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_mfa_backup_codes_credential_id"), table_name="mfa_backup_codes")
    op.drop_table("mfa_backup_codes")
    op.drop_index(op.f("ix_mfa_credentials_user_id"), table_name="mfa_credentials")
    op.drop_table("mfa_credentials")
