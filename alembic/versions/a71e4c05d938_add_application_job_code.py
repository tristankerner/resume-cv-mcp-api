"""add a job code to applications

A requisition code, optional, free-form: `REQ-12345`, `R2026-887`, whatever
the posting or the recruiter calls it. `normalized_job_code` is the same value
with case and separators removed (see `Normalizer.job_code`), which is what
matching reads — two recruiters putting the same requisition forward rarely
spell it the same way, and identifying that is the reason the field exists.

**Recreates the SQLite audit triggers on `applications`.** SQLite names its
audited columns literally, so the triggers written by `4b0715f7197e` do not
know about these two columns and would keep writing an audit row that omits
them. This is the known limitation that migration's docstring warns about, and
this is what answering it looks like. Postgres needs nothing: its trigger
function works from `to_jsonb(NEW)` minus an excluded-column list, so it picks
up a new column on its own.

Revision ID: a71e4c05d938
Revises: 9c14ab30f6d2
Create Date: 2026-09-07 20:05:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op
from services.tracking.audit_triggers import AuditTriggers

# revision identifiers, used by Alembic.
revision: str = "a71e4c05d938"
down_revision: Union[str, Sequence[str], None] = "9c14ab30f6d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEX_NAME = "ix_applications_user_job_code"

NEW_COLUMNS = ("job_code", "normalized_job_code")

# Pinned rather than read from `AuditColumns`, for the reason given in
# `4b0715f7197e`: a migration states the schema at its own point in history.
# `AFTER` is what `applications` looks like once this revision has run;
# `BEFORE` is what it looked like before, which the downgrade has to restore.
AUDITED_AFTER: list[str] = [
    "id",
    "user_id",
    "company_id",
    "url",
    "job_title",
    "normalized_job_title",
    "job_code",
    "normalized_job_code",
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
]

AUDITED_BEFORE: list[str] = [
    column for column in AUDITED_AFTER if column not in NEW_COLUMNS
]


def _rewrite_sqlite_triggers(columns: list[str]) -> None:
    for statement in AuditTriggers.drop_sqlite("applications"):
        op.execute(statement)
    for statement in AuditTriggers.sqlite_triggers(
        "applications", "id", "user_id", columns
    ):
        op.execute(statement)


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("applications", sa.Column("job_code", sa.String(), nullable=True))
    op.add_column(
        "applications", sa.Column("normalized_job_code", sa.String(), nullable=True)
    )
    op.create_index(INDEX_NAME, "applications", ["user_id", "normalized_job_code"])

    if op.get_bind().dialect.name != "postgresql":
        _rewrite_sqlite_triggers(AUDITED_AFTER)


def downgrade() -> None:
    """Downgrade schema."""
    # Triggers first: they name the two columns, and SQLite resolves a trigger
    # body when it fires rather than when it is created, so leaving them in
    # place would turn the next write to `applications` into a runtime error
    # about a column that is no longer there.
    if op.get_bind().dialect.name != "postgresql":
        _rewrite_sqlite_triggers(AUDITED_BEFORE)

    op.drop_index(INDEX_NAME, table_name="applications")
    op.drop_column("applications", "normalized_job_code")
    op.drop_column("applications", "job_code")
