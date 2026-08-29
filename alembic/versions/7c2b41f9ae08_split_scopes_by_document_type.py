"""split scopes by document type, and replace the mcp role with member

Three changes to the authorization data, none of them to the schema:

1. One scope per document type per verb. `resume:read:private` becomes
   `resume:read` and gains `metadata:read` and `skill:read` beside it, so a
   credential can be handed the tailoring instructions without the résumé.
   `resume:read:public` is deleted outright: the published projection takes no
   credential, so nothing ever checked it.
2. A `member` role, holding every document scope and not `users:admin`.
3. The `mcp` role is gone. It gave a machine client a read-only identity back
   when documents were one shared set; now that a document has an owner, an
   MCP client authenticates as that owner with a narrowed API key.

Existing grants are widened to preserve what they could already do, rather
than left to fail closed. The old `resume:*` scopes gated all three document
types — the split is what makes them type-specific — so a key holding
`resume:read:private` is given `metadata:read` and `skill:read`, and the same
for write and delete. This does grant stored credentials scopes they were not
minted with, deliberately and only as far as their existing reach. It cannot
overshoot: `AuthService._authenticate_api_key` intersects a key's scopes with
its owner's role scopes on every request, so a widened key still cannot exceed
the person it belongs to.

Users holding `mcp` are moved to `member` for the same reason: the role is
being removed under them, and leaving it would expand to nothing and lock them
out of their own documents.

Revision ID: 7c2b41f9ae08
Revises: 35808d4a4c1d
Create Date: 2026-08-25 14:00:00.000000

"""
import json
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c2b41f9ae08"
down_revision: Union[str, Sequence[str], None] = "35808d4a4c1d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DOCUMENT_SCOPES = [
    "resume:read",
    "resume:write",
    "resume:delete",
    "metadata:read",
    "metadata:write",
    "metadata:delete",
    "skill:read",
    "skill:write",
    "skill:delete",
]

SEED = {
    "admin": [*DOCUMENT_SCOPES, "users:admin"],
    "member": list(DOCUMENT_SCOPES),
}

# Old scope -> what a credential holding it could previously do. The old
# `resume:*` family gated every document type, so preserving reach means
# fanning each one out across all three.
WIDENED = {
    "resume:read:private": ["resume:read", "metadata:read", "skill:read"],
    "resume:write": ["resume:write", "metadata:write", "skill:write"],
    "resume:delete": ["resume:delete", "metadata:delete", "skill:delete"],
}

# Checked nowhere, so it granted nothing. Dropped rather than mapped.
RETIRED = {"resume:read:public"}

# The reverse map, for the downgrade. `resume:read` alone is enough to say a
# credential previously held `resume:read:private`, since that is where it came
# from; the metadata and skill scopes beside it collapse back into the same one.
NARROWED = {
    "resume:read": "resume:read:private",
    "metadata:read": "resume:read:private",
    "skill:read": "resume:read:private",
    "resume:write": "resume:write",
    "metadata:write": "resume:write",
    "skill:write": "resume:write",
    "resume:delete": "resume:delete",
    "metadata:delete": "resume:delete",
    "skill:delete": "resume:delete",
}


role_scopes_table = sa.table(
    "role_scopes", sa.column("role", sa.String()), sa.column("scope", sa.String())
)


def _rewrite_json_lists(conn, table: str, column: str, rewrite) -> None:
    """Apply `rewrite` to every row's JSON list, in Python.

    The lists live in JSON columns, and the two dialects disagree on how to
    take one apart in SQL. These are tables of tens of rows on a personal
    deployment, so reading them out and writing them back is both portable and
    fast enough — and it keeps the mapping above readable as a dict.
    """
    rows = conn.execute(
        sa.text(f"SELECT id, {column} FROM {table}")  # noqa: S608 - fixed identifiers
    ).fetchall()
    for row in rows:
        current = row[1]
        if isinstance(current, str):
            current = json.loads(current)
        updated = rewrite(list(current or []))
        if updated == list(current or []):
            continue
        conn.execute(
            sa.text(f"UPDATE {table} SET {column} = :value WHERE id = :id"),  # noqa: S608
            {"value": json.dumps(updated), "id": row[0]},
        )


def _widen(values: list[str]) -> list[str]:
    widened: list[str] = []
    for value in values:
        if value in RETIRED:
            continue
        for replacement in WIDENED.get(value, [value]):
            if replacement not in widened:
                widened.append(replacement)
    return widened


def _narrow(values: list[str]) -> list[str]:
    narrowed: list[str] = []
    for value in values:
        replacement = NARROWED.get(value, value)
        if replacement not in narrowed:
            narrowed.append(replacement)
    return narrowed


def _replace_role(values: list[str], old: str, new: str) -> list[str]:
    if old not in values:
        return values
    replaced = [new if role == old else role for role in values]
    deduped: list[str] = []
    for role in replaced:
        if role not in deduped:
            deduped.append(role)
    return deduped


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()

    # Custom roles an operator added by hand are carried across too, rather
    # than silently losing the scope they were given.
    for old, replacements in WIDENED.items():
        for replacement in replacements:
            conn.execute(
                sa.text(
                    "INSERT INTO role_scopes (role, scope) "
                    "SELECT role, :new FROM role_scopes WHERE scope = :old "
                    "AND role NOT IN ('admin', 'mcp', 'member')"
                ),
                {"new": replacement, "old": old},
            )
    conn.execute(
        sa.text("DELETE FROM role_scopes WHERE scope IN :retired").bindparams(
            sa.bindparam("retired", value=sorted(RETIRED | set(WIDENED)), expanding=True)
        )
    )

    conn.execute(
        sa.text("DELETE FROM role_scopes WHERE role IN ('admin', 'mcp', 'member')")
    )
    op.bulk_insert(
        role_scopes_table,
        [
            {"role": role, "scope": scope}
            for role, scopes in SEED.items()
            for scope in scopes
        ],
    )

    _rewrite_json_lists(conn, "api_keys", "scopes", _widen)
    _rewrite_json_lists(
        conn, "users", "roles", lambda roles: _replace_role(roles, "mcp", "member")
    )


def downgrade() -> None:
    """Downgrade schema.

    Lossy in one direction that cannot be helped: a credential given
    `skill:read` alone since the upgrade collapses back to the old
    `resume:read:private`, which reads every type. Narrowing is not
    representable in a vocabulary that had no per-type scopes.
    """
    conn = op.get_bind()

    for new, old in NARROWED.items():
        conn.execute(
            sa.text(
                "INSERT INTO role_scopes (role, scope) "
                "SELECT DISTINCT role, :old FROM role_scopes WHERE scope = :new "
                "AND role NOT IN ('admin', 'mcp', 'member') "
                "AND role NOT IN (SELECT role FROM role_scopes WHERE scope = :old)"
            ),
            {"new": new, "old": old},
        )
    conn.execute(
        sa.text("DELETE FROM role_scopes WHERE scope IN :split").bindparams(
            sa.bindparam("split", value=sorted(NARROWED), expanding=True)
        )
    )

    conn.execute(
        sa.text("DELETE FROM role_scopes WHERE role IN ('admin', 'mcp', 'member')")
    )
    op.bulk_insert(
        role_scopes_table,
        [
            {"role": "admin", "scope": scope}
            for scope in (
                "resume:read:public",
                "resume:read:private",
                "resume:write",
                "resume:delete",
                "users:admin",
            )
        ]
        + [
            {"role": "mcp", "scope": scope}
            for scope in ("resume:read:public", "resume:read:private")
        ],
    )

    _rewrite_json_lists(conn, "api_keys", "scopes", _narrow)
    _rewrite_json_lists(
        conn, "users", "roles", lambda roles: _replace_role(roles, "member", "mcp")
    )
