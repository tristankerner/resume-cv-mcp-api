"""add passkey credentials

A new table, `passkey_credentials` (one row per enrolled WebAuthn
credential), plus `users.webauthn_user_handle` — the opaque id an
authenticator hands back in a discoverable-credential assertion. The
challenge between the two ceremony steps is a signed JWT, not a database row,
so there is no `passkey_challenges` table — see services/auth/passkeys/
challenge.py, the same shape as the MFA challenge in f3106ced7318.

Purely additive, so a migration applied ahead of the deploy is readable by the
code already running in front of it — the same property
c1e5a7d92b30_add_login_throttling.py's docstring calls out, true here too.

`passkey_credentials` is **not audited**, deliberately, joining
`mfa_credentials` and `mfa_backup_codes` in the list 4b0715f7197e's docstring
gives for that reason: it is either churn (`sign_count`, `last_used_at`) or a
public key with nothing secret to leak.

**Recreates the SQLite audit triggers on `users`.** `webauthn_user_handle` is
new on an audited table, and SQLite's triggers name their audited columns
literally — the same limitation cecabf408169 answered for `timezone`, worked
the same way here. Postgres needs nothing: its trigger function works from
`to_jsonb(NEW)` minus an excluded-column list, so it picks up a new column on
its own.

Revision ID: bec5c0b76281
Revises: d980e94de5a2
Create Date: 2026-09-10 13:07:21.490125

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op
from services.tracking.audit_triggers import AuditTriggers

# revision identifiers, used by Alembic.
revision: str = "bec5c0b76281"
down_revision: Union[str, Sequence[str], None] = "d980e94de5a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_COLUMNS = ("webauthn_user_handle",)

# Pinned rather than read from `AuditColumns` at run time, for the reason
# given in `4b0715f7197e` and repeated in `cecabf408169`: a migration states
# the schema at its own point in history. `AFTER` is what `users` looks like
# once this revision has run; `BEFORE` is what it looked like before, which
# the downgrade has to restore.
AUDITED_AFTER: list[str] = [
    "id",
    "username",
    "email",
    "first_name",
    "last_name",
    "roles",
    "active",
    "timezone",
    "failed_login_count",
    "first_failed_login_at",
    "locked_until",
    "lock_count",
    "locked_permanently_at",
    "webauthn_user_handle",
]

AUDITED_BEFORE: list[str] = [
    column for column in AUDITED_AFTER if column not in NEW_COLUMNS
]


def _rewrite_sqlite_triggers(columns: list[str]) -> None:
    for statement in AuditTriggers.drop_sqlite("users"):
        op.execute(statement)
    for statement in AuditTriggers.sqlite_triggers("users", "id", "id", columns):
        op.execute(statement)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "passkey_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("credential_id", sa.String(), nullable=False),
        sa.Column("public_key", sa.String(), nullable=False),
        sa.Column("sign_count", sa.Integer(), nullable=False),
        sa.Column("transports", sa.JSON(), nullable=False),
        sa.Column("aaguid", sa.String(), nullable=True),
        sa.Column("device_type", sa.String(), nullable=False),
        sa.Column("backed_up", sa.Boolean(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_passkey_credentials_user_id"), "passkey_credentials", ["user_id"]
    )
    op.create_index(
        op.f("ix_passkey_credentials_credential_id"),
        "passkey_credentials",
        ["credential_id"],
        unique=True,
    )

    op.add_column(
        "users", sa.Column("webauthn_user_handle", sa.String(), nullable=True)
    )
    op.create_index(
        op.f("ix_users_webauthn_user_handle"),
        "users",
        ["webauthn_user_handle"],
        unique=True,
    )

    if op.get_bind().dialect.name != "postgresql":
        _rewrite_sqlite_triggers(AUDITED_AFTER)


def downgrade() -> None:
    """Downgrade schema."""
    # Triggers first: they name `webauthn_user_handle`, and SQLite resolves a
    # trigger body when it fires rather than when it is created, so leaving
    # them in place would turn the next write to `users` into a runtime error
    # about a column that is no longer there.
    if op.get_bind().dialect.name != "postgresql":
        _rewrite_sqlite_triggers(AUDITED_BEFORE)

    op.drop_index(op.f("ix_users_webauthn_user_handle"), table_name="users")
    op.drop_column("users", "webauthn_user_handle")

    op.drop_index(
        op.f("ix_passkey_credentials_credential_id"), table_name="passkey_credentials"
    )
    op.drop_index(
        op.f("ix_passkey_credentials_user_id"), table_name="passkey_credentials"
    )
    op.drop_table("passkey_credentials")
