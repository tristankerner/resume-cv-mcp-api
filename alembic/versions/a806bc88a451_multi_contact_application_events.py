"""multi contact application events

An application event may now name any number of contacts instead of at most
one. `application_events.contact_id` is dropped and replaced by the join
table `application_event_contacts` (minimal: `id, user_id, event_id,
contact_id, created_at`, unique on `(event_id, contact_id)`).

A downgrade can only restore **one** contact per event, because that is all
the column ever held. Every event that named two or more contacts loses all
but the first (the lowest-id join row - see `ApplicationEventContact`'s
ordering rule) when downgrading. This is inherent to the direction of the
change, not a defect in this migration.

`deprecated_application_event_contact_ids` is an immutable snapshot of
`application_events.contact_id` taken the instant before the column is
dropped - a migration artifact with no SQLAlchemy model, declared inline the
same way `a17c4e9b2d50` declares `document_schema`. It is not read at run
time and is not dropped by this migration; dropping it is a deliberate
follow-up revision to be written once the change is verified in production.

**Rebuilds the SQLite audit triggers on `application_events`, and creates
them for `application_event_contacts`.** SQLite names its audited columns
literally (see `AuditTriggers`'s docstring), so both dropping a column from
an audited table and creating a newly audited table require this. The
triggers are dropped before the column, in the upgrade, and recreated before
the column is restored, in the downgrade - SQLite resolves a trigger body
when it fires, not when it is created, so leaving a trigger naming a column
that is gone would turn the next ordinary write into a runtime error with no
obvious cause.

Revision ID: a806bc88a451
Revises: a17c4e9b2d50
Create Date: 2026-09-15 21:09:19.918712

"""

from datetime import UTC, datetime
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op
from services.tracking.audit_triggers import AuditTriggers

# revision identifiers, used by Alembic.
revision: str = "a806bc88a451"
down_revision: Union[str, Sequence[str], None] = "a17c4e9b2d50"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEX_NAME = "ix_application_events_user_contact"

# Pinned rather than read from `AuditColumns` at run time, for the reason
# given in `4b0715f7197e` and repeated in every later revision that touches
# an audited table: a migration states the schema at its own point in
# history. `AFTER` is what `application_events` looks like once this
# revision has run; `BEFORE` is what it looked like before, which the
# downgrade has to restore.
APPLICATION_EVENTS_AFTER: list[str] = [
    "id",
    "user_id",
    "application_id",
    "status",
    "description",
    "rating",
    "occurred_at",
    "created_at",
]

APPLICATION_EVENTS_BEFORE: list[str] = [
    "id",
    "user_id",
    "application_id",
    "status",
    "contact_id",
    "description",
    "rating",
    "occurred_at",
    "created_at",
]

APPLICATION_EVENT_CONTACTS_COLUMNS: list[str] = [
    "id",
    "user_id",
    "event_id",
    "contact_id",
    "created_at",
]


def _deprecated_backup_table() -> sa.Table:
    return sa.table(
        "deprecated_application_event_contact_ids",
        sa.column("event_id", sa.Integer()),
        sa.column("contact_id", sa.Integer()),
        sa.column("captured_at", sa.DateTime()),
    )


def _application_events_table() -> sa.Table:
    return sa.table(
        "application_events",
        sa.column("id", sa.Integer()),
        sa.column("user_id", sa.Integer()),
        sa.column("contact_id", sa.Integer()),
        sa.column("created_at", sa.DateTime()),
    )


def _application_event_contacts_table() -> sa.Table:
    return sa.table(
        "application_event_contacts",
        sa.column("id", sa.Integer()),
        sa.column("user_id", sa.Integer()),
        sa.column("event_id", sa.Integer()),
        sa.column("contact_id", sa.Integer()),
        sa.column("created_at", sa.DateTime()),
    )


def _events_copy_from() -> sa.Table:
    """`application_events` exactly as it stands before this revision, for
    batch mode's table rebuild.

    Everything the rebuilt table must keep has to be named here. Batch mode
    with `copy_from` does not reflect: it builds the new table from this
    definition alone, so an omitted CHECK constraint (which alembic cannot
    reflect out of SQLite in the first place) or an omitted index is dropped
    with the old table and never comes back.
    `ix_application_events_user_contact` is deliberately absent - it names
    the column being dropped.
    """
    return sa.Table(
        "application_events",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "application_id",
            sa.Integer(),
            sa.ForeignKey("applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column(
            "contact_id",
            sa.Integer(),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "rating IS NULL OR (rating BETWEEN 1 AND 10)",
            name="ck_application_events_rating_range",
        ),
        sa.Index(
            "ix_application_events_user_app_occurred",
            "user_id",
            "application_id",
            "occurred_at",
        ),
        sa.Index("ix_application_events_user_occurred", "user_id", "occurred_at"),
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "deprecated_application_event_contact_ids",
        sa.Column("event_id", sa.Integer(), primary_key=True),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "application_event_contacts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "event_id",
            sa.Integer(),
            sa.ForeignKey("application_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "contact_id",
            sa.Integer(),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "event_id", "contact_id", name="uq_application_event_contacts_pair"
        ),
    )
    op.create_index(
        "ix_application_event_contacts_user_event",
        "application_event_contacts",
        ["user_id", "event_id"],
    )
    op.create_index(
        "ix_application_event_contacts_user_contact",
        "application_event_contacts",
        ["user_id", "contact_id"],
    )

    now = datetime.now(UTC).replace(tzinfo=None)
    events = _application_events_table()
    backup = _deprecated_backup_table()
    op.execute(
        backup.insert().from_select(
            ["event_id", "contact_id", "captured_at"],
            sa.select(
                events.c.id,
                events.c.contact_id,
                sa.literal(now, type_=sa.DateTime()),
            ).where(events.c.contact_id.is_not(None)),
        )
    )

    join_table = _application_event_contacts_table()
    op.execute(
        join_table.insert().from_select(
            ["user_id", "event_id", "contact_id", "created_at"],
            sa.select(
                events.c.user_id,
                events.c.id,
                events.c.contact_id,
                events.c.created_at,
            ).where(events.c.contact_id.is_not(None)),
        )
    )

    # Must precede the column drop: SQLite refuses to drop a column an index
    # references.
    op.drop_index(INDEX_NAME, table_name="application_events")

    if op.get_bind().dialect.name != "postgresql":
        for statement in AuditTriggers.drop_sqlite("application_events"):
            op.execute(statement)

    if op.get_bind().dialect.name == "postgresql":
        op.drop_column("application_events", "contact_id")
    else:
        # SQLite's native `DROP COLUMN` refuses a column named in a
        # table-level `FOREIGN KEY`, which is exactly how SQLAlchemy renders
        # `contact_id`'s `ForeignKey("contacts.id", ...)` - so this is not a
        # fallback for an unlikely case, it is the only route SQLite has. A
        # full table rebuild through batch mode, with the table spelled out;
        # see `_events_copy_from` for what that costs.
        with op.batch_alter_table(
            "application_events", copy_from=_events_copy_from()
        ) as batch_op:
            batch_op.drop_column("contact_id")

    if op.get_bind().dialect.name != "postgresql":
        for statement in AuditTriggers.sqlite_triggers(
            "application_events", "id", "user_id", APPLICATION_EVENTS_AFTER
        ):
            op.execute(statement)

        for statement in AuditTriggers.sqlite_triggers(
            "application_event_contacts",
            "id",
            "user_id",
            APPLICATION_EVENT_CONTACTS_COLUMNS,
        ):
            op.execute(statement)
    else:
        op.execute(
            AuditTriggers.postgres_trigger("application_event_contacts", "id", "user_id")
        )


def downgrade() -> None:
    """Downgrade schema."""
    if op.get_bind().dialect.name != "postgresql":
        for statement in AuditTriggers.drop_sqlite("application_events"):
            op.execute(statement)
        for statement in AuditTriggers.drop_sqlite("application_event_contacts"):
            op.execute(statement)

    op.add_column(
        "application_events", sa.Column("contact_id", sa.Integer(), nullable=True)
    )
    if op.get_bind().dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_application_events_contact_id",
            "application_events",
            "contacts",
            ["contact_id"],
            ["id"],
            ondelete="SET NULL",
        )
    # SQLite cannot add a foreign key to an existing table without a full
    # table rebuild, and does not enforce foreign keys in this codebase
    # anyway (rule 0.7) - so the restored column is left FK-less there.

    events = _application_events_table()
    join_table = _application_event_contacts_table()
    backup = _deprecated_backup_table()
    lowest_join_contact = (
        sa.select(join_table.c.contact_id)
        .where(join_table.c.event_id == events.c.id)
        .order_by(join_table.c.id)
        .limit(1)
        .scalar_subquery()
    )
    snapshot_contact = (
        sa.select(backup.c.contact_id)
        .where(backup.c.event_id == events.c.id)
        .scalar_subquery()
    )
    op.execute(
        events.update().values(
            contact_id=sa.func.coalesce(lowest_join_contact, snapshot_contact)
        )
    )

    if op.get_bind().dialect.name != "postgresql":
        for statement in AuditTriggers.sqlite_triggers(
            "application_events", "id", "user_id", APPLICATION_EVENTS_BEFORE
        ):
            op.execute(statement)

    op.create_index(
        INDEX_NAME, "application_events", ["user_id", "contact_id"]
    )

    op.drop_index(
        "ix_application_event_contacts_user_contact",
        table_name="application_event_contacts",
    )
    op.drop_index(
        "ix_application_event_contacts_user_event",
        table_name="application_event_contacts",
    )
    op.drop_table("application_event_contacts")
    op.drop_table("deprecated_application_event_contact_ids")
