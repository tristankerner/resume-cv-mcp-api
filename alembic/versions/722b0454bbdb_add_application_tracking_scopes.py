"""add application tracking scopes

Grants the ten new tracking scopes (`applications:*`, `companies:*`,
`contacts:*`, `audit:read`) to both `admin` and `member` - tracking data is
owned per-row, same as documents, so the role distinction stays exactly what
it already was: whether the caller may administer users.

Idempotent: a `NOT EXISTS` guard on each insert means a re-run adds nothing
new. Only the `admin` and `member` rows are touched; a custom role added by
hand is left alone.

Revision ID: 722b0454bbdb
Revises: d523332cc7aa
Create Date: 2026-09-07 14:12:12.637396

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '722b0454bbdb'
down_revision: Union[str, Sequence[str], None] = 'd523332cc7aa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_SCOPES = [
    "applications:read",
    "applications:write",
    "applications:delete",
    "companies:read",
    "companies:write",
    "companies:delete",
    "contacts:read",
    "contacts:write",
    "contacts:delete",
    "audit:read",
]

ROLES = ["admin", "member"]


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    for role in ROLES:
        for scope in NEW_SCOPES:
            # The two values are CAST explicitly because Postgres cannot
            # deduce a parameter's type when the same placeholder appears both
            # as a bare SELECT target and compared against a column: it infers
            # `text` from the former and `character varying` from the latter
            # and refuses the statement with "inconsistent types deduced for
            # parameter $1". SQLite does not care either way, so without the
            # cast this migration passes the suite and fails the deployment.
            conn.execute(
                sa.text(
                    "INSERT INTO role_scopes (role, scope) "
                    "SELECT CAST(:role AS VARCHAR), CAST(:scope AS VARCHAR) "
                    "WHERE NOT EXISTS ("
                    "SELECT 1 FROM role_scopes WHERE role = :role AND scope = :scope)"
                ),
                {"role": role, "scope": scope},
            )


def downgrade() -> None:
    """Downgrade schema.

    Also strips the retired scopes out of every stored API key. Leaving them
    there would be harmless today — `ScopeResolver.parse` drops a scope the
    enum no longer has, and `narrow` intersects with the owner's current role
    scopes on every request — but a key that still records a capability the
    server has removed misreports what it can do everywhere it is listed.
    """
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM role_scopes WHERE role IN :roles AND scope IN :scopes").bindparams(
            sa.bindparam("roles", value=ROLES, expanding=True),
            sa.bindparam("scopes", value=NEW_SCOPES, expanding=True),
        )
    )

    # Read out and written back in Python: the lists live in JSON columns and
    # the two dialects disagree on how to take one apart in SQL. Same approach
    # as `7c2b41f9ae08._rewrite_json_lists`, which this mirrors.
    retired = set(NEW_SCOPES)
    rows = conn.execute(sa.text("SELECT id, scopes FROM api_keys")).fetchall()
    for row in rows:
        current = row[1]
        if isinstance(current, str):
            current = json.loads(current)
        current = list(current or [])
        remaining = [scope for scope in current if scope not in retired]
        if remaining == current:
            continue
        conn.execute(
            sa.text("UPDATE api_keys SET scopes = :value WHERE id = :id"),
            {"value": json.dumps(remaining), "id": row[0]},
        )
