"""add audit log

Column-allowlist audit triggers on ten tables, plus `audit_log` and
`audit_actor` (SQLite's stand-in for a Postgres transaction-local setting -
see `persistence/audit_actor.py`).

**Audited, full row:** `applications`, `application_events`, `companies`,
`company_relationships`, `company_stack_items`, `contacts`.
**Audited, columns allowlisted:** `application_attachments` (everything but
`content_base64`), `users` (everything but `password`), `api_keys`
(everything but `key_hash`), `oauth_clients` (everything but
`client_secret_hash`).

**Not audited, deliberately:** `mfa_credentials`, `mfa_backup_codes`,
`oauth_authorization_codes`, `oauth_refresh_tokens`, `auth_failures` (all
secret-bearing or pure churn), `documents` (already append-only, so its own
table is its history), `document_schema` and `role_scopes` (reference data
owned by migrations), `audit_log` and `audit_actor` themselves (would
recurse).

The whole column-allowlist lives in `AuditColumns.AUDITED`, once, and the
trigger bodies are generated from it by `AuditTriggers` — shared with any
later migration that has to recreate a table's triggers, so the two cannot
drift apart by hand.

**Known limitation**, restated from section 11.6 of the tracking plan:
SQLite's triggers name their audited columns literally. Any future migration
that adds, drops or renames a column on one of these tables - or that
rebuilds the table with `batch_alter_table` - must drop and recreate that
table's three SQLite triggers, or they will silently stop matching the live
schema. `tests/test_audit_log.py`'s structural test is the alarm for this:
it fails the day a column is added to an audited table's `__table_args__`
without a matching migration update here.

Revision ID: 4b0715f7197e
Revises: 722b0454bbdb
Create Date: 2026-09-07 15:40:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op
from services.tracking.audit_columns import AuditColumns
from services.tracking.audit_triggers import AuditTriggers

# revision identifiers, used by Alembic.
revision: str = "4b0715f7197e"
down_revision: Union[str, Sequence[str], None] = "722b0454bbdb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The allowlist **as it stood at this revision**, written out rather than
# imported from `AuditColumns`. A migration is a snapshot of a schema at a
# point in history: reading the live allowlist would mean replaying this
# revision on a fresh database generated triggers naming columns that later
# revisions add, which is a trigger that is wrong for as long as it takes the
# next migration to run. `services/tracking/audit_columns.py` stays the
# current truth; this is what was true here.
#
# A later migration that changes an audited table's columns recreates that
# table's triggers with its own list - see a71e4c05d938.
AUDITED: dict[str, tuple[str, str | None, list[str]]] = {
    "applications": (
        "id",
        "user_id",
        [
            "id",
            "user_id",
            "company_id",
            "url",
            "job_title",
            "normalized_job_title",
            "resume_document_name",
            "resume_revision_id",
            "metadata_document_name",
            "metadata_revision_id",
            "skill_document_name",
            "skill_revision_id",
            "resume_label",
            "initial_prompt_text",
            "job_description",
            "date_submitted",
            "manually_modified",
            "modification_note",
            "source",
            "system",
            "status",
            "status_changed_at",
            "created_at",
            "updated_at",
        ],
    ),
    "application_events": (
        "id",
        "user_id",
        [
            "id",
            "user_id",
            "application_id",
            "status",
            "contact_id",
            "description",
            "rating",
            "occurred_at",
            "created_at",
        ],
    ),
    "application_attachments": (
        "id",
        "user_id",
        [
            "id",
            "user_id",
            "application_id",
            "kind",
            "filename",
            "content_type",
            "byte_size",
            "sha256",
            "created_at",
        ],
    ),
    "companies": (
        "id",
        "user_id",
        [
            "id",
            "user_id",
            "name",
            "normalized_name",
            "website",
            "description",
            "personal_note",
            "created_at",
            "updated_at",
        ],
    ),
    "company_relationships": (
        "id",
        "user_id",
        [
            "id",
            "user_id",
            "from_company_id",
            "to_company_id",
            "type",
            "note",
            "created_at",
        ],
    ),
    "company_stack_items": (
        "id",
        "user_id",
        [
            "id",
            "user_id",
            "company_id",
            "name",
            "normalized_name",
            "type",
            "description",
            "created_at",
            "updated_at",
        ],
    ),
    "contacts": (
        "id",
        "user_id",
        [
            "id",
            "user_id",
            "company_id",
            "first_name",
            "last_name",
            "normalized_name",
            "email",
            "phone",
            "description",
            "personal_note",
            "rating",
            "created_at",
            "updated_at",
        ],
    ),
    "users": (
        "id",
        "id",
        [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "roles",
            "active",
            "failed_login_count",
            "first_failed_login_at",
            "locked_until",
            "lock_count",
            "locked_permanently_at",
        ],
    ),
    "api_keys": (
        "id",
        "user_id",
        [
            "id",
            "user_id",
            "name",
            "prefix",
            "scopes",
            "created_at",
            "last_used_at",
            "expires_at",
            "revoked_at",
        ],
    ),
    "oauth_clients": (
        "id",
        None,
        [
            "id",
            "client_id",
            "client_name",
            "redirect_uris",
            "grant_types",
            "response_types",
            "token_endpoint_auth_method",
            "scope",
            "created_at",
            "client_secret_expires_at",
        ],
    ),
}

EXCLUDED_COLUMNS = AuditColumns.EXCLUDED


def _create_audit_tables() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("table_name", sa.String(), nullable=False),
        sa.Column("row_pk", sa.String(), nullable=False),
        sa.Column("operation", sa.String(length=1), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("row_user_id", sa.Integer(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("actor_credential", sa.String(), nullable=True),
        sa.Column("old_data", sa.JSON(), nullable=True),
        sa.Column("new_data", sa.JSON(), nullable=True),
        sa.Column("changed_columns", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_audit_log_table_row", "audit_log", ["table_name", "row_pk", "changed_at"]
    )
    op.create_index("ix_audit_log_row_user", "audit_log", ["row_user_id", "changed_at"])
    op.create_index("ix_audit_log_actor", "audit_log", ["actor_user_id", "changed_at"])

    op.create_table(
        "audit_actor",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("actor_credential", sa.String(), nullable=True),
        sa.Column("set_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_audit_actor_single_row"),
    )


def upgrade() -> None:
    """Upgrade schema."""
    _create_audit_tables()
    dialect = op.get_bind().dialect.name

    if dialect == "postgresql":
        op.execute(AuditTriggers.postgres_function())
        for table, (pk_col, user_col, _columns) in AUDITED.items():
            op.execute(AuditTriggers.postgres_trigger(table, pk_col, user_col))
    else:
        for table, (pk_col, user_col, columns) in AUDITED.items():
            insert_sql, update_sql, delete_sql = AuditTriggers.sqlite_triggers(
                table, pk_col, user_col, columns
            )
            op.execute(insert_sql)
            op.execute(update_sql)
            op.execute(delete_sql)


def downgrade() -> None:
    """Downgrade schema."""
    dialect = op.get_bind().dialect.name

    if dialect == "postgresql":
        for table in AUDITED:
            op.execute(f"DROP TRIGGER IF EXISTS audit_{table} ON {table}")  # noqa: S608
        op.execute("DROP FUNCTION IF EXISTS audit_row_change()")
    else:
        for table in AUDITED:
            for statement in AuditTriggers.drop_sqlite(table):
                op.execute(statement)

    op.drop_table("audit_actor")
    op.drop_table("audit_log")
