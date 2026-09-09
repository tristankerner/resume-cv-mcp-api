"""add user timezone

A nullable IANA name (`"America/Chicago"`); `NULL` means UTC. Display-only —
the API always emits UTC and this column exists so the client knows what to
convert to. See `services/common/datetimes.py` for the wire-format half of
this feature.

**Recreates the SQLite audit triggers on `users`.** SQLite names its audited
columns literally, so the triggers written by `4b0715f7197e` have no idea
`timezone` exists and would keep writing audit rows that silently omit it.
This is the same limitation `a71e4c05d938` answered for `applications`, worked
the same way here. Postgres needs nothing: its trigger function works from
`to_jsonb(NEW)` minus an excluded-column list, so it picks up a new column on
its own.

Revision ID: cecabf408169
Revises: c0d3b18e4a52
Create Date: 2026-09-09 07:36:00.304095

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op
from services.tracking.audit_triggers import AuditTriggers

# revision identifiers, used by Alembic.
revision: str = "cecabf408169"
down_revision: Union[str, Sequence[str], None] = "c0d3b18e4a52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_COLUMNS = ("timezone",)

# Pinned rather than read from `AuditColumns` at run time, for the reason
# given in `4b0715f7197e` and repeated in `a71e4c05d938`: a migration states
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
    op.add_column("users", sa.Column("timezone", sa.String(), nullable=True))

    if op.get_bind().dialect.name != "postgresql":
        _rewrite_sqlite_triggers(AUDITED_AFTER)


def downgrade() -> None:
    """Downgrade schema."""
    # Triggers first: they name `timezone`, and SQLite resolves a trigger body
    # when it fires rather than when it is created, so leaving them in place
    # would turn the next write to `users` into a runtime error about a
    # column that is no longer there.
    if op.get_bind().dialect.name != "postgresql":
        _rewrite_sqlite_triggers(AUDITED_BEFORE)

    op.drop_column("users", "timezone")
