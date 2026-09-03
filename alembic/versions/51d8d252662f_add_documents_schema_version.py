"""add documents schema version

Adds `documents.schema_version`, the plain integer recording which
`document_schema` row a stored revision was written against, and a composite
foreign key `(type, schema_version) -> document_schema(document_type,
version)` enforcing that every row names a version this build's catalogue
actually knows about.

Sequenced to avoid a table rebuild where one is not needed:

1. `op.add_column` with `NOT NULL` and `server_default="1"` in one shot.
   SQLite's `ALTER TABLE ADD COLUMN` accepts a NOT NULL column when a
   non-null default is supplied, so this alone needs no batch mode — the
   no-UPDATE trigger `35808d4a4c1d` recreated survives untouched. The
   add-nullable / backfill / tighten dance that revision used is not needed
   here: there is nothing to backfill from, every existing row simply becomes
   schema 1.
2. The server default is left in place afterwards, on both dialects, rather
   than dropped on Postgres. `services/document/document_types.py` stamps
   the current version explicitly on every write from Phase 6 onward, so the
   default is inert in practice; keeping it is one less dialect branch, and a
   column that is NOT NULL with no default would make a raw `INSERT` from a
   tool that does not know about this column fail loudly instead of reading
   1, which is a worse failure mode for a column this migration is adding
   underneath already-written code.
3. Before the foreign key: assert every distinct `documents.type` already has
   a version-1 `document_schema` row. `DocumentTypeRegistry.scopes_for_stored`
   exists precisely because a stored row may carry a type this build does not
   know about (retired, or written by a newer build); a composite FK would
   make such a row un-insertable and make *this* migration fail on an
   existing one. Failing loudly here, naming the type, is the intended
   outcome per `SCHEMA_VERSIONING_PLAN.md` decision 3 — the same reasoning
   `35808d4a4c1d._backfill_created_by` already applies to a missing admin.
4. The foreign key itself: a plain `op.create_foreign_key` on Postgres. On
   SQLite, adding a foreign key is a table rebuild regardless of `batch_op`
   convenience, which drops any trigger defined on the table — so it goes
   through the same drop/recreate-the-no-update-trigger dance
   `35808d4a4c1d` uses (there is no no-delete trigger left to restore; that
   one was retired there too). Copied rather than imported — migrations do
   not import each other, see that revision's docstring.

Revision ID: 51d8d252662f
Revises: cb6cd1ae433b
Create Date: 2026-09-03 08:56:31.693853

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "51d8d252662f"
down_revision: Union[str, Sequence[str], None] = "cb6cd1ae433b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FK_NAME = "fk_documents_type_schema_version"


def _drop_no_update_trigger(dialect: str) -> None:
    if dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS ts_documents_no_update ON documents")
        op.execute("DROP FUNCTION IF EXISTS documents_no_update()")
    else:
        op.execute("DROP TRIGGER IF EXISTS ts_documents_no_update")


def _recreate_no_update_trigger(dialect: str) -> None:
    """Copied from `35808d4a4c1d._recreate_no_update_trigger` — migrations do
    not import each other, see that revision's docstring."""
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


def _documents_with_schema_version() -> sa.Table:
    """`documents` as it looks once `schema_version` has been added but
    before the foreign key — the basis batch mode rebuilds from to add the
    FK. See `35808d4a4c1d` for why a literal Table is used instead of
    reflection: reflecting here would hand batch mode the two-column primary
    key it long ago stopped having, is not the issue this time, but building
    a fresh `MetaData()` per revision is what keeps each migration
    self-contained.
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
        sa.Column(
            "schema_version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.PrimaryKeyConstraint("created_by", "name", "revision_id"),
    )


def _documents_without_schema_version() -> sa.Table:
    """The shape this revision started from, for the downgrade's rebuild."""
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


def _assert_every_type_is_catalogued(bind: sa.Connection) -> None:
    stored_types = {
        row[0] for row in bind.execute(sa.text("SELECT DISTINCT type FROM documents"))
    }
    catalogued_types = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT document_type FROM document_schema WHERE version = 1")
        )
    }
    unknown = stored_types - catalogued_types
    if unknown:
        raise RuntimeError(
            "Cannot add the documents -> document_schema foreign key: "
            f"documents.type contains {sorted(unknown)!r}, which has no "
            "version-1 row in document_schema. This build does not "
            "recognise that type; add a catalogue row for it, or resolve "
            "the stray rows, before running this migration."
        )


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.add_column(
        "documents",
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
    )

    _assert_every_type_is_catalogued(bind)

    if dialect == "postgresql":
        op.create_foreign_key(
            FK_NAME,
            "documents",
            "document_schema",
            ["type", "schema_version"],
            ["document_type", "version"],
        )
    else:
        _drop_no_update_trigger(dialect)
        with op.batch_alter_table(
            "documents",
            recreate="always",
            copy_from=_documents_with_schema_version(),
        ) as batch_op:
            batch_op.create_foreign_key(
                FK_NAME,
                "document_schema",
                ["type", "schema_version"],
                ["document_type", "version"],
            )
        _recreate_no_update_trigger(dialect)


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.drop_constraint(FK_NAME, "documents", type_="foreignkey")
        op.drop_column("documents", "schema_version")
    else:
        _drop_no_update_trigger(dialect)
        with op.batch_alter_table(
            "documents",
            recreate="always",
            copy_from=_documents_without_schema_version(),
        ):
            pass
        _recreate_no_update_trigger(dialect)
