"""document ownership and types

Turns `documents` from a single shared store into one where every row belongs
to a user: adds `created_by`, `type`, `public` and `created_at`, and rebuilds
the primary key to `(created_by, name, revision_id)` so two users may each
hold a document called the same thing.

Sequenced carefully:

1. Drop both append-only triggers first. On SQLite the table rebuild that
   batch mode performs below would not carry them forward; on Postgres they
   would fire during the backfill UPDATE.
2. Add the four columns nullable.
3. Backfill them from what the old, name-gated schema already implied:
   `type` from the name (`resume.json` -> `resume`, etc; anything
   unrecognised defaults to `resume` rather than blocking the NOT NULL below —
   nothing unrecognised could actually exist, since `DocumentName` gated
   writes until now), `public` true only for `resume.json` (preserving
   `PUBLIC_DOCUMENTS`'s old behaviour exactly), `created_at` to "now" in each
   dialect's own idea of UTC, and `created_by` to the lowest-id admin user —
   raising if there are document rows and no admin, since a wrong owner is
   worse than a failed migration.
4. Set NOT NULL on all four.
5. Rebuild the primary key.
6. Add the FK to `users` and the `(created_by, type)` index.
7. Recreate only the no-update trigger. The no-delete trigger does not come
   back — deletion is a supported operation from here on.

**Deploy-window note.** `deploy/03-migrate.sh` runs this against the live
database before the new image deploys. In the minute or two between those two
steps, the old code serves reads fine but cannot write: the old
`upsert_document` inserts no `created_by`/`type` and hits the NOT NULL added
here. Writes are admin-only on a personal service, so this is accepted rather
than done as two revisions (ship nullable, backfill, tighten later) — that
two-deploy pattern is the right call once this serves more than one operator.

Revision ID: 35808d4a4c1d
Revises: 0d7b385e6a45
Create Date: 2026-08-25 12:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "35808d4a4c1d"
down_revision: Union[str, Sequence[str], None] = "0d7b385e6a45"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _drop_append_only_triggers(dialect: str) -> None:
    if dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS ts_documents_no_update ON documents")
        op.execute("DROP TRIGGER IF EXISTS ts_documents_no_delete ON documents")
        op.execute("DROP FUNCTION IF EXISTS documents_no_update()")
        op.execute("DROP FUNCTION IF EXISTS documents_no_delete()")
    else:
        op.execute("DROP TRIGGER IF EXISTS ts_documents_no_update")
        op.execute("DROP TRIGGER IF EXISTS ts_documents_no_delete")


def _recreate_no_update_trigger(dialect: str) -> None:
    if dialect == "postgresql":
        op.execute("""
            CREATE OR REPLACE FUNCTION documents_no_update()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'Updates are forbidden on append-only table: documents';
            END;
            $$ LANGUAGE plpgsql;
        """)
        op.execute("""
            CREATE TRIGGER ts_documents_no_update
            BEFORE UPDATE ON documents
            FOR EACH ROW EXECUTE FUNCTION documents_no_update();
        """)
    else:
        op.execute("""
            CREATE TRIGGER IF NOT EXISTS ts_documents_no_update
            BEFORE UPDATE ON documents
            BEGIN
                SELECT RAISE(FAIL, 'Updates are forbidden on append-only table: documents');
            END;
        """)


def _recreate_no_delete_trigger(dialect: str) -> None:
    """Only used by the downgrade — restores what f74c464133e8 originally had."""
    if dialect == "postgresql":
        op.execute("""
            CREATE OR REPLACE FUNCTION documents_no_delete()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'Deletions are forbidden on append-only table: documents';
            END;
            $$ LANGUAGE plpgsql;
        """)
        op.execute("""
            CREATE TRIGGER ts_documents_no_delete
            BEFORE DELETE ON documents
            FOR EACH ROW EXECUTE FUNCTION documents_no_delete();
        """)
    else:
        op.execute("""
            CREATE TRIGGER IF NOT EXISTS ts_documents_no_delete
            BEFORE DELETE ON documents
            BEGIN
                SELECT RAISE(FAIL, 'Deletions are forbidden on append-only table: documents');
            END;
        """)


def _documents_after_upgrade() -> sa.Table:
    """What `documents` should look like once this revision has run.

    Handed to batch mode as `copy_from` rather than letting it reflect: the
    reflected table still carries the old two-column primary key, and asking
    batch mode to build a three-column one on top of that reflection raises a
    SAWarning that says it "may become an exception in a future release" —
    not something to leave sitting in a migration that has to keep working.
    Column order matches the live table, which is what the INSERT ... SELECT
    that batch mode generates copies through.
    """
    return sa.Table(
        "documents",
        sa.MetaData(),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("revision_note", sa.String(), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("public", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("created_by", "name", "revision_id"),
    )


def _documents_before_upgrade() -> sa.Table:
    """The shape this revision started from, for the downgrade's own recreate.

    Same reasoning as above, in reverse: reflection would hand batch mode the
    three-column key it is about to replace.
    """
    return sa.Table(
        "documents",
        sa.MetaData(),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("revision_id", sa.Integer(), nullable=False),
        sa.Column("revision_note", sa.String(), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("name", "revision_id"),
    )


def _backfill_created_by(conn) -> None:
    doc_count = conn.execute(sa.text("SELECT COUNT(*) FROM documents")).scalar()
    if not doc_count:
        return

    users_table = sa.table(
        "users", sa.column("id", sa.Integer()), sa.column("roles", sa.JSON())
    )
    rows = conn.execute(
        sa.select(users_table.c.id, users_table.c.roles).order_by(users_table.c.id)
    ).fetchall()
    admin_id = next(
        (row.id for row in rows if row.roles and "admin" in row.roles), None
    )
    if admin_id is None:
        raise RuntimeError(
            "Cannot backfill documents.created_by: document rows exist but no "
            "user holds the admin role. Grant one the role and re-run this "
            "migration."
        )
    conn.execute(
        sa.text("UPDATE documents SET created_by = :admin_id"),
        {"admin_id": admin_id},
    )


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    _drop_append_only_triggers(dialect)

    op.add_column("documents", sa.Column("type", sa.String(), nullable=True))
    op.add_column("documents", sa.Column("public", sa.Boolean(), nullable=True))
    op.add_column("documents", sa.Column("created_by", sa.Integer(), nullable=True))
    op.add_column("documents", sa.Column("created_at", sa.DateTime(), nullable=True))

    op.execute("""
        UPDATE documents SET type = CASE
            WHEN name = 'resume.json' THEN 'resume'
            WHEN name = 'resume.metadata.json' THEN 'metadata'
            WHEN name = 'resume.skill.json' THEN 'skill'
            ELSE 'resume'
        END
    """)
    # Preserves PUBLIC_DOCUMENTS' old behaviour exactly: it was the frozenset
    # {resume.json}, and that column is being deleted.
    op.execute("UPDATE documents SET public = (name = 'resume.json')")
    if dialect == "postgresql":
        # Postgres' CURRENT_TIMESTAMP is tz-aware; the column is naive UTC by
        # convention (see persistence/base.utcnow), so it is not used here.
        op.execute("UPDATE documents SET created_at = (now() at time zone 'utc')")
    else:
        op.execute("UPDATE documents SET created_at = CURRENT_TIMESTAMP")
    _backfill_created_by(bind)

    if dialect == "postgresql":
        op.alter_column("documents", "type", existing_type=sa.String(), nullable=False)
        op.alter_column(
            "documents",
            "public",
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        )
        op.alter_column(
            "documents", "created_by", existing_type=sa.Integer(), nullable=False
        )
        op.alter_column(
            "documents", "created_at", existing_type=sa.DateTime(), nullable=False
        )

        op.drop_constraint("documents_pkey", "documents", type_="primary")
        op.create_primary_key(
            "documents_pkey", "documents", ["created_by", "name", "revision_id"]
        )
        op.create_foreign_key(
            "fk_documents_created_by_users",
            "documents",
            "users",
            ["created_by"],
            ["id"],
        )
        op.create_index(
            "ix_documents_created_by_type", "documents", ["created_by", "type"]
        )
    else:
        # The NOT NULLs and the new primary key are all carried by `copy_from`
        # — batch mode rebuilds the table from that definition — so only the
        # constraint and index it does not describe are issued as ops here.
        with op.batch_alter_table(
            "documents", recreate="always", copy_from=_documents_after_upgrade()
        ) as batch_op:
            batch_op.create_foreign_key(
                "fk_documents_created_by_users", "users", ["created_by"], ["id"]
            )
            batch_op.create_index(
                "ix_documents_created_by_type", ["created_by", "type"]
            )

    _recreate_no_update_trigger(dialect)


def downgrade() -> None:
    """Downgrade schema.

    Lossy: collapsing the primary key back to (name, revision_id) cannot
    represent two different owners holding a document with the same name and
    revision number. Refused outright if that has happened rather than
    silently dropping rows.
    """
    bind = op.get_bind()
    dialect = bind.dialect.name

    collision = bind.execute(
        sa.text("""
            SELECT name, revision_id
            FROM documents
            GROUP BY name, revision_id
            HAVING COUNT(DISTINCT created_by) > 1
        """)
    ).first()
    if collision is not None:
        raise RuntimeError(
            "Cannot downgrade: multiple owners hold a document with the same "
            "name and revision_id, which the pre-ownership primary key cannot "
            "represent. Resolve the collision by hand before downgrading."
        )

    if dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS ts_documents_no_update ON documents")
        op.execute("DROP FUNCTION IF EXISTS documents_no_update()")
    else:
        op.execute("DROP TRIGGER IF EXISTS ts_documents_no_update")

    if dialect == "postgresql":
        op.drop_constraint(
            "fk_documents_created_by_users", "documents", type_="foreignkey"
        )
        op.drop_index("ix_documents_created_by_type", table_name="documents")
        op.drop_constraint("documents_pkey", "documents", type_="primary")
        op.create_primary_key("documents_pkey", "documents", ["name", "revision_id"])
        op.drop_column("documents", "type")
        op.drop_column("documents", "public")
        op.drop_column("documents", "created_by")
        op.drop_column("documents", "created_at")
    else:
        # `copy_from` describes the table this is returning to, so the rebuild
        # drops the four columns, the foreign key and the index along with the
        # ownership half of the primary key in one pass.
        op.drop_index("ix_documents_created_by_type", table_name="documents")
        with op.batch_alter_table(
            "documents", recreate="always", copy_from=_documents_before_upgrade()
        ):
            pass

    _recreate_no_update_trigger(dialect)
    _recreate_no_delete_trigger(dialect)
