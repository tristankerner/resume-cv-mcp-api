from typing import ClassVar

from services.tracking.audit_columns import AuditColumns


class AuditTriggers:
    """Generates the audit trigger SQL for both dialects from `AuditColumns`.

    Lives here rather than inside the migration that first created the
    triggers because it has more than one caller. SQLite names its audited
    columns literally, so **any migration that adds, drops or renames a column
    on an audited table has to drop and recreate that table's three triggers**
    — and a second copy of this generator is exactly how the two would drift
    into disagreeing about what is audited.

    Postgres needs none of that: its one `audit_row_change` function works
    from `to_jsonb(NEW)` minus an excluded-column list, so a new column is
    picked up automatically. The asymmetry is deliberate and is why
    `AuditColumns.AUDITED` (an allowlist, for SQLite) and
    `AuditColumns.EXCLUDED` (a denylist, for Postgres) must stay
    complementary — `tests/test_audit_log.py` holds them to it.
    """

    OPERATIONS: ClassVar[tuple[str, ...]] = ("insert", "update", "delete")

    @staticmethod
    def postgres_function() -> str:
        return """
        CREATE OR REPLACE FUNCTION audit_row_change() RETURNS trigger AS $$
        DECLARE
            pk_col      text := TG_ARGV[0];
            user_col    text := NULLIF(TG_ARGV[1], '');
            excluded    text[] := TG_ARGV[2:];
            old_j       jsonb;
            new_j       jsonb;
            changed     jsonb;
            actor       int;
            actor_cred  text;
        BEGIN
            IF TG_OP <> 'INSERT' THEN old_j := to_jsonb(OLD) - excluded; END IF;
            IF TG_OP <> 'DELETE' THEN new_j := to_jsonb(NEW) - excluded; END IF;

            IF TG_OP = 'UPDATE' THEN
                SELECT jsonb_agg(key ORDER BY key) INTO changed
                FROM jsonb_each(new_j)
                WHERE new_j -> key IS DISTINCT FROM old_j -> key;
                IF changed IS NULL THEN RETURN NULL; END IF;
            END IF;

            actor := NULLIF(current_setting('app.actor_user_id', true), '')::int;
            actor_cred := NULLIF(current_setting('app.actor_credential', true), '');

            INSERT INTO audit_log (
                table_name, row_pk, operation, changed_at,
                row_user_id, actor_user_id, actor_credential,
                old_data, new_data, changed_columns
            ) VALUES (
                TG_TABLE_NAME,
                COALESCE(new_j ->> pk_col, old_j ->> pk_col),
                LEFT(TG_OP, 1),
                (now() AT TIME ZONE 'utc'),
                CASE WHEN user_col IS NULL THEN NULL
                     ELSE COALESCE((new_j ->> user_col)::int, (old_j ->> user_col)::int) END,
                actor, actor_cred,
                old_j, new_j, changed
            );
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """

    @staticmethod
    def postgres_trigger(table: str, pk_col: str, user_col: str | None) -> str:
        excluded = AuditColumns.EXCLUDED.get(table, [])
        args = ", ".join(
            [f"'{pk_col}'", f"'{user_col or ''}'"] + [f"'{col}'" for col in excluded]
        )
        return f"""
        CREATE TRIGGER audit_{table}
        AFTER INSERT OR UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION audit_row_change({args});
    """

    @staticmethod
    def _json_object(columns: list[str], row: str) -> str:
        return ", ".join(f"'{col}', {row}.{col}" for col in columns)

    @staticmethod
    def _changed_union(columns: list[str]) -> str:
        # `IS NOT` is SQLite's null-safe inequality, so an unchanged NULL does
        # not register as a change.
        return " UNION ALL ".join(
            f"SELECT '{col}' AS col WHERE NEW.{col} IS NOT OLD.{col}" for col in columns
        )

    # The `# nosec B608` markers below are not a shrug at SQL injection. Every
    # value interpolated into these statements - the table name, the primary
    # key, the owning column, each audited column - comes from the literal
    # `AuditColumns.AUDITED` dict or from a migration's own pinned list, never
    # from a request. There is no parameterised form of `CREATE TRIGGER`, so
    # string construction is the only way to write one at all.
    @classmethod
    def sqlite_triggers(
        cls, table: str, pk_col: str, user_col: str | None, columns: list[str]
    ) -> tuple[str, str, str]:
        new_json = cls._json_object(columns, "NEW")
        old_json = cls._json_object(columns, "OLD")
        changed_union = cls._changed_union(columns)
        new_user = f"NEW.{user_col}" if user_col else "NULL"
        old_user = f"OLD.{user_col}" if user_col else "NULL"

        insert_sql = f"""
        CREATE TRIGGER IF NOT EXISTS audit_{table}_insert
        AFTER INSERT ON {table}
        BEGIN
            INSERT INTO audit_log (table_name, row_pk, operation, changed_at,
                row_user_id, actor_user_id, actor_credential,
                old_data, new_data, changed_columns)
            VALUES ('{table}', CAST(NEW.{pk_col} AS TEXT), 'I',
                strftime('%Y-%m-%d %H:%M:%f', 'now'),
                {new_user},
                (SELECT actor_user_id FROM audit_actor WHERE id = 1),
                (SELECT actor_credential FROM audit_actor WHERE id = 1),
                NULL,
                json_object({new_json}),
                NULL);
        END;
    """  # nosec B608 - fixed identifiers, see the note above

        update_sql = f"""
        CREATE TRIGGER IF NOT EXISTS audit_{table}_update
        AFTER UPDATE ON {table}
        BEGIN
            INSERT INTO audit_log (table_name, row_pk, operation, changed_at,
                row_user_id, actor_user_id, actor_credential,
                old_data, new_data, changed_columns)
            SELECT '{table}', CAST(NEW.{pk_col} AS TEXT), 'U',
                strftime('%Y-%m-%d %H:%M:%f', 'now'),
                {new_user},
                (SELECT actor_user_id FROM audit_actor WHERE id = 1),
                (SELECT actor_credential FROM audit_actor WHERE id = 1),
                json_object({old_json}),
                json_object({new_json}),
                changed.cols
            FROM (SELECT json_group_array(col) AS cols FROM ({changed_union})) AS changed
            WHERE json_array_length(changed.cols) > 0;
        END;
    """  # nosec B608 - fixed identifiers, see the note above

        delete_sql = f"""
        CREATE TRIGGER IF NOT EXISTS audit_{table}_delete
        AFTER DELETE ON {table}
        BEGIN
            INSERT INTO audit_log (table_name, row_pk, operation, changed_at,
                row_user_id, actor_user_id, actor_credential,
                old_data, new_data, changed_columns)
            VALUES ('{table}', CAST(OLD.{pk_col} AS TEXT), 'D',
                strftime('%Y-%m-%d %H:%M:%f', 'now'),
                {old_user},
                (SELECT actor_user_id FROM audit_actor WHERE id = 1),
                (SELECT actor_credential FROM audit_actor WHERE id = 1),
                json_object({old_json}),
                NULL,
                NULL);
        END;
    """  # nosec B608 - fixed identifiers, see the note above

        return insert_sql, update_sql, delete_sql

    @classmethod
    def drop_sqlite(cls, table: str) -> list[str]:
        return [
            f"DROP TRIGGER IF EXISTS audit_{table}_{operation}"
            for operation in cls.OPERATIONS
        ]

    # There is deliberately no `recreate_from_current_allowlist` helper. A
    # migration that recreates a table's triggers passes its own pinned column
    # list to `sqlite_triggers`, because it is restoring the schema as of its
    # own revision — reading `AuditColumns.AUDITED` at run time would make
    # replaying history generate triggers for columns that do not exist yet.
    # See a71e4c05d938.
