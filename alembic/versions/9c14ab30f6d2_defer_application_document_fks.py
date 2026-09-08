"""defer the application -> document revision foreign keys

`applications` carries three composite foreign keys into
`documents(created_by, name, revision_id)`. Renaming a document is a clone of
every revision under the new name followed by a delete of the originals — see
`Document.rename` — and `DocumentService.rename_document` retargets the
referencing applications in the same transaction.

Checked immediately, that transaction cannot be ordered to satisfy the
constraint: SQLAlchemy's unit of work flushes the `applications` UPDATE before
the cloned `documents` rows are inserted, so Postgres rejects the rename with

    insert or update on table "applications" violates foreign key constraint
    "fk_applications_skill_revision"

and the caller gets a 500. Deferring the check to COMMIT makes the ordering
within the transaction irrelevant, which is the property the rename and delete
paths actually need — the constraint still holds at every point an outside
observer can see.

SQLite is untouched: it does not enforce foreign keys in this application
(`PRAGMA foreign_keys` is off, deliberately — see the tracking plan) and has no
`ALTER TABLE ... ALTER CONSTRAINT`. The guarantee there is
`DocumentService`'s own bookkeeping, same as before.

Revision ID: 9c14ab30f6d2
Revises: eb599ee50641
Create Date: 2026-09-07 19:20:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c14ab30f6d2"
down_revision: Union[str, Sequence[str], None] = "eb599ee50641"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CONSTRAINTS = (
    "fk_applications_resume_revision",
    "fk_applications_metadata_revision",
    "fk_applications_skill_revision",
)


def upgrade() -> None:
    """Upgrade schema."""
    if op.get_bind().dialect.name != "postgresql":
        return
    for name in CONSTRAINTS:
        op.execute(
            f"ALTER TABLE applications ALTER CONSTRAINT {name} "  # noqa: S608
            "DEFERRABLE INITIALLY DEFERRED"
        )


def downgrade() -> None:
    """Downgrade schema."""
    if op.get_bind().dialect.name != "postgresql":
        return
    for name in CONSTRAINTS:
        op.execute(
            f"ALTER TABLE applications ALTER CONSTRAINT {name} NOT DEFERRABLE"  # noqa: S608
        )
