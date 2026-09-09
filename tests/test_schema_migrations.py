"""Upgrade *and* downgrade coverage for every revision
`SCHEMA_VERSIONING_PLAN.md` added, against a dedicated, function-scoped
SQLite database.

`tests/conftest.py`'s session-scoped `migrated_database` fixture runs
`alembic upgrade head` once and never downgrades, so it exercises every
migration's upgrade path for free but its downgrade path not at all — and
downgrading the shared session database mid-suite would break every test
that runs after it. This module builds its own temporary database per test
instead, and drives `alembic.command.upgrade` / `.downgrade` directly.

Only fictional, hand-built fixtures appear here — never anything from
`data/v2-migration/`, which is real personal data and gitignored. The
byte-for-byte "converts to exactly `data/v2-migration/resume.v2.json`"
verification is a manual, one-off check against that real data, reported to
the human rather than encoded as a committed test (see the final report).
"""

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

REPO_ROOT = Path(__file__).resolve().parent.parent

PHASE_1 = "cb6cd1ae433b"  # add document_schema table + v1 rows
PHASE_2 = "51d8d252662f"  # add documents.schema_version
PHASE_3 = "bcc6cfe85c34"  # add v2 document_schema rows
PHASE_4 = "b0baccd6558a"  # convert resume documents to v2
PHASE_5 = "78480d80f589"  # convert metadata/skill documents to v2
BEFORE_ALL = "f3106ced7318"  # head immediately before this plan's work

USER_TIMEZONE = "cecabf408169"  # add users.timezone + rebuild SQLite audit triggers
BEFORE_USER_TIMEZONE = "c0d3b18e4a52"  # head immediately before that revision


@pytest.fixture
def migration_db(monkeypatch):
    """A throwaway SQLite database, isolated from the shared session
    database `tests/conftest.py` builds. Yields (sqlite3 connection factory,
    alembic Config) — never used through `DatabaseService`, since nothing
    here needs the application, only the migrations themselves."""
    from services.config.config_service import ConfigService

    tmpdir = tempfile.mkdtemp(prefix="resume-cv-mcp-api-migration-test-")
    db_path = Path(tmpdir) / "migration_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    ConfigService.reset()

    cfg = Config(str(REPO_ROOT / "alembic.ini"))

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(db_path)

    yield connect, cfg

    ConfigService.reset()


def _insert_user(conn: sqlite3.Connection, admin: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO users (username, password, roles, active) VALUES (?,?,?,?)",
        ("admin", "x", json.dumps(["admin"] if admin else []), 1),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def _insert_document(
    conn: sqlite3.Connection,
    *,
    created_by: int,
    name: str,
    revision_id: int,
    doc_type: str,
    data: dict,
    schema_version: int | None = None,
    public: bool = False,
) -> None:
    if schema_version is None:
        conn.execute(
            "INSERT INTO documents (created_by, name, revision_id, type, public, "
            "created_at, revision_note, data) VALUES (?,?,?,?,?,?,?,?)",
            (
                created_by,
                name,
                revision_id,
                doc_type,
                int(public),
                "2026-01-01 00:00:00",
                "seed",
                json.dumps(data),
            ),
        )
    else:
        conn.execute(
            "INSERT INTO documents (created_by, name, revision_id, type, public, "
            "created_at, revision_note, data, schema_version) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                created_by,
                name,
                revision_id,
                doc_type,
                int(public),
                "2026-01-01 00:00:00",
                "seed",
                json.dumps(data),
                schema_version,
            ),
        )
    conn.commit()


class TestPhase1DocumentSchemaTable:
    def test_round_trips_and_seeds_three_v1_rows(self, migration_db):
        connect, cfg = migration_db
        command.upgrade(cfg, PHASE_1)

        conn = connect()
        rows = conn.execute(
            "SELECT version, document_type, json_schema FROM document_schema "
            "ORDER BY document_type"
        ).fetchall()
        assert [(v, t) for v, t, _ in rows] == [
            (1, "metadata"),
            (1, "resume"),
            (1, "skill"),
        ]

        from services.document.dtos.resume_object import ResumeMetadata
        from services.document.dtos.resume_skill import ResumeSkill

        by_type = {t: json.loads(schema) for _, t, schema in rows}
        assert by_type["metadata"] == ResumeMetadata.model_json_schema()
        assert by_type["skill"] == ResumeSkill.model_json_schema()
        conn.close()

        command.downgrade(cfg, BEFORE_ALL)
        conn = connect()
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "document_schema" not in tables
        conn.close()


class TestPhase2SchemaVersionColumn:
    def test_backfills_existing_rows_and_round_trips(self, migration_db):
        connect, cfg = migration_db
        command.upgrade(cfg, PHASE_1)

        conn = connect()
        owner = _insert_user(conn)
        _insert_document(
            conn,
            created_by=owner,
            name="resume.json",
            revision_id=1,
            doc_type="resume",
            data={"anything": True},
        )
        conn.close()

        command.upgrade(cfg, PHASE_2)
        conn = connect()
        version = conn.execute(
            "SELECT schema_version FROM documents WHERE name='resume.json'"
        ).fetchone()[0]
        assert version == 1

        triggers = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        assert "ts_documents_no_update" in triggers
        conn.close()

        command.downgrade(cfg, PHASE_1)
        conn = connect()
        columns = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
        assert "schema_version" not in columns
        triggers = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        assert "ts_documents_no_update" in triggers
        conn.close()

    def test_refuses_the_foreign_key_for_an_unknown_type(self, migration_db):
        connect, cfg = migration_db
        command.upgrade(cfg, PHASE_1)
        conn = connect()
        owner = _insert_user(conn)
        _insert_document(
            conn,
            created_by=owner,
            name="mystery.json",
            revision_id=1,
            doc_type="retired-type-nobody-knows",
            data={},
        )
        conn.close()

        with pytest.raises(Exception, match="no version-1 row in document_schema"):
            command.upgrade(cfg, PHASE_2)


class TestPhase3V2CatalogueRows:
    def test_six_rows_total_and_round_trips(self, migration_db):
        connect, cfg = migration_db
        command.upgrade(cfg, PHASE_3)

        conn = connect()
        rows = conn.execute(
            "SELECT version, document_type FROM document_schema ORDER BY version, document_type"
        ).fetchall()
        assert rows == [
            (1, "metadata"),
            (1, "resume"),
            (1, "skill"),
            (2, "metadata"),
            (2, "resume"),
            (2, "skill"),
        ]

        from services.document.dtos.resume_object import ResumePrivate

        schema = json.loads(
            conn.execute(
                "SELECT json_schema FROM document_schema WHERE version=2 AND document_type='resume'"
            ).fetchone()[0]
        )
        assert schema == ResumePrivate.model_json_schema()
        conn.close()

        command.downgrade(cfg, PHASE_2)
        conn = connect()
        count = conn.execute("SELECT COUNT(*) FROM document_schema").fetchone()[0]
        assert count == 3
        conn.close()


FICTIONAL_V1_RESUME = {
    "profile": {"name": "Fictional Person", "title": "Engineer", "tagline": "tag"},
    "contact": {
        "email_address": "fictional@example.invalid",
        "mobile_number": "555-0100",
        "locations": [
            {"label": "Nowhere", "kind": "remote", "note": "", "publish": True}
        ],
        "links": [],
    },
    "summary": "A fictional summary.",
    "skill_groups": [
        {
            "name": "Languages",
            "publish": True,
            "skills": [
                {
                    "name": "Python",
                    "url": None,
                    "level": "expert",
                    "last_used": "2026",
                    "publish": True,
                }
            ],
        }
    ],
    "certifications": [],
    "jobs": [
        {
            "company": "Fictional Co",
            "company_url": None,
            "company_location": "Remote",
            "start": "2020-01",
            "end": None,
            "via_employer": None,
            "description": "does things",
            "role_location": "Remote",
            "roles": [{"title": "Engineer", "start": "2020", "end": None}],
            "highlights": [
                {
                    "id": "h1",
                    "summary": "Did a fictional thing",
                    "specifics": ["one detail"],
                    "tech": ["Python"],
                    "metrics": [],
                    "story": None,
                    "publish": True,
                }
            ],
            "publish": True,
        }
    ],
    "education": [],
    "personal_projects": [],
    "fine_tuning_data": None,
}


class TestPhase4ResumeConversion:
    def test_converts_audits_and_downgrades(self, migration_db):
        connect, cfg = migration_db
        command.upgrade(cfg, PHASE_3)
        conn = connect()
        owner = _insert_user(conn)
        _insert_document(
            conn,
            created_by=owner,
            name="resume.json",
            revision_id=1,
            doc_type="resume",
            data=FICTIONAL_V1_RESUME,
            schema_version=1,
            public=True,
        )
        conn.close()

        command.upgrade(cfg, PHASE_4)
        conn = connect()
        rows = conn.execute(
            "SELECT revision_id, schema_version, data FROM documents "
            "WHERE name='resume.json' ORDER BY revision_id"
        ).fetchall()
        assert [r[0:2] for r in rows] == [(1, 1), (2, 2)]
        converted = json.loads(rows[1][2])
        assert converted["basics"]["name"] == "Fictional Person"
        assert converted["basics"]["email"] == "fictional@example.invalid"
        assert converted["work"][0]["name"] == "Fictional Co"
        assert (
            converted["work"][0]["highlights"][0]["specifics"][0]["detail"]
            == "one detail"
        )
        conn.close()

        command.downgrade(cfg, PHASE_3)
        conn = connect()
        remaining = conn.execute(
            "SELECT revision_id FROM documents WHERE name='resume.json'"
        ).fetchall()
        assert remaining == [(1,)]
        conn.close()

    def test_downgrade_refuses_above_a_newer_revision(self, migration_db):
        connect, cfg = migration_db
        command.upgrade(cfg, PHASE_3)
        conn = connect()
        owner = _insert_user(conn)
        _insert_document(
            conn,
            created_by=owner,
            name="resume.json",
            revision_id=1,
            doc_type="resume",
            data=FICTIONAL_V1_RESUME,
            schema_version=1,
        )
        conn.close()
        command.upgrade(cfg, PHASE_4)

        conn = connect()
        # Simulate a user writing a new revision after the deploy.
        _insert_document(
            conn,
            created_by=owner,
            name="resume.json",
            revision_id=3,
            doc_type="resume",
            data={"basics": {"name": "Edited later"}},
            schema_version=2,
        )
        conn.close()

        with pytest.raises(Exception, match="newer revision"):
            command.downgrade(cfg, PHASE_3)

    def test_a_corrupted_document_aborts_with_zero_writes(self, migration_db):
        connect, cfg = migration_db
        command.upgrade(cfg, PHASE_3)
        broken = {**FICTIONAL_V1_RESUME, "jobs": [{"no": "company key"}]}
        conn = connect()
        owner = _insert_user(conn)
        _insert_document(
            conn,
            created_by=owner,
            name="resume.json",
            revision_id=1,
            doc_type="resume",
            data=broken,
            schema_version=1,
        )
        conn.close()

        with pytest.raises(Exception):  # noqa: B017 - the migration's own exception type
            command.upgrade(cfg, PHASE_4)

        conn = connect()
        rows = conn.execute(
            "SELECT revision_id FROM documents WHERE name='resume.json'"
        ).fetchall()
        assert rows == [(1,)]
        current = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert current == (PHASE_3,)
        conn.close()


class TestPhase5MetadataAndSkillConversion:
    def _v1_metadata_seeded(self) -> dict:
        path = REPO_ROOT / "alembic/versions/schema_snapshots/v1_metadata.seeded.json"
        return json.loads(path.read_text())

    def _v2_metadata_example(self) -> dict:
        path = REPO_ROOT / "alembic/versions/schema_snapshots/v2_metadata.example.json"
        return json.loads(path.read_text())

    def test_untouched_document_is_replaced_with_the_v2_example(self, migration_db):
        connect, cfg = migration_db
        command.upgrade(cfg, PHASE_4)
        seeded = self._v1_metadata_seeded()
        conn = connect()
        owner = _insert_user(conn)
        _insert_document(
            conn,
            created_by=owner,
            name="resume.metadata.json",
            revision_id=1,
            doc_type="metadata",
            data=seeded,
            schema_version=1,
        )
        conn.close()

        command.upgrade(cfg, PHASE_5)
        conn = connect()
        row = conn.execute(
            "SELECT data, schema_version FROM documents WHERE revision_id=2"
        ).fetchone()
        assert json.loads(row[0]) == self._v2_metadata_example()
        assert row[1] == 2
        conn.close()

        command.downgrade(cfg, PHASE_4)
        conn = connect()
        remaining = conn.execute(
            "SELECT revision_id FROM documents WHERE name='resume.metadata.json'"
        ).fetchall()
        assert remaining == [(1,)]
        conn.close()

    def test_customised_document_keeps_edits_and_gains_v2_content(self, migration_db):
        connect, cfg = migration_db
        command.upgrade(cfg, PHASE_4)
        customised = self._v1_metadata_seeded()
        customised["disclosure"]["rules"][0] = "A fictional custom rule."
        conn = connect()
        owner = _insert_user(conn)
        _insert_document(
            conn,
            created_by=owner,
            name="resume.metadata.json",
            revision_id=1,
            doc_type="metadata",
            data=customised,
            schema_version=1,
        )
        conn.close()

        command.upgrade(cfg, PHASE_5)
        conn = connect()
        result = json.loads(
            conn.execute("SELECT data FROM documents WHERE revision_id=2").fetchone()[0]
        )
        conn.close()

        assert result["disclosure"]["rules"][0] == "A fictional custom rule."
        v2_example = self._v2_metadata_example()
        assert (
            result["disclosure"]["never_publish"]
            == v2_example["disclosure"]["never_publish"]
        )
        v2_vocab_paths = {v["path"] for v in v2_example["vocabularies"]}
        result_vocab_paths = {v["path"] for v in result["vocabularies"]}
        assert v2_vocab_paths <= result_vocab_paths

    def test_unmapped_path_stops_the_migration(self, migration_db):
        connect, cfg = migration_db
        command.upgrade(cfg, PHASE_4)
        broken = self._v1_metadata_seeded()
        broken["sections"][0]["fields"][0]["path"] = "resume.does_not_exist_anywhere"
        conn = connect()
        owner = _insert_user(conn)
        _insert_document(
            conn,
            created_by=owner,
            name="resume.metadata.json",
            revision_id=1,
            doc_type="metadata",
            data=broken,
            schema_version=1,
        )
        conn.close()

        with pytest.raises(Exception):  # noqa: B017
            command.upgrade(cfg, PHASE_5)

        conn = connect()
        rows = conn.execute(
            "SELECT revision_id FROM documents WHERE name='resume.metadata.json'"
        ).fetchall()
        assert rows == [(1,)]
        current = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert current == (PHASE_4,)
        conn.close()


class TestAddUserTimezone:
    """`cecabf408169` - a nullable `users.timezone`, and the SQLite audit
    trigger rebuild that a new column on an audited table requires. See
    `a71e4c05d938` for the same shape on `applications`."""

    def test_new_column_is_audited(self, migration_db):
        connect, cfg = migration_db
        command.upgrade(cfg, USER_TIMEZONE)
        conn = connect()
        conn.execute(
            "INSERT INTO users (username, password, roles, active, timezone) "
            "VALUES (?, ?, ?, ?, ?)",
            ("admin", "x", json.dumps(["admin"]), 1, "America/Chicago"),
        )
        conn.commit()
        new_data = conn.execute(
            "SELECT new_data FROM audit_log WHERE table_name='users' AND operation='I'"
        ).fetchone()[0]
        conn.close()
        assert json.loads(new_data)["timezone"] == "America/Chicago"

    def test_updating_only_timezone_changes_only_timezone(self, migration_db):
        """Proves the `IS NOT` union was rebuilt with `timezone` in it, not
        just the `json_object` - a trigger that only gained the column in its
        snapshot but not in its change-detection union would report every
        audited column as changed on every update."""
        connect, cfg = migration_db
        command.upgrade(cfg, USER_TIMEZONE)
        conn = connect()
        conn.execute(
            "INSERT INTO users (username, password, roles, active, timezone) "
            "VALUES (?, ?, ?, ?, ?)",
            ("admin", "x", json.dumps(["admin"]), 1, None),
        )
        conn.commit()
        conn.execute(
            "UPDATE users SET timezone = ? WHERE username = 'admin'",
            ("America/New_York",),
        )
        conn.commit()
        changed = conn.execute(
            "SELECT changed_columns FROM audit_log WHERE table_name='users' "
            "AND operation='U'"
        ).fetchone()[0]
        conn.close()
        assert json.loads(changed) == ["timezone"]

    def test_downgrade_rebuilds_triggers_and_writes_still_succeed(self, migration_db):
        """A downgrade that left the `timezone`-naming trigger in place would
        turn the next ordinary write to `users` into a runtime error about a
        column that no longer exists - SQLite resolves a trigger body when it
        fires, not when it is created. This is the failure a naive "did the
        migration run" check would miss."""
        connect, cfg = migration_db
        command.upgrade(cfg, USER_TIMEZONE)
        conn = connect()
        conn.execute(
            "INSERT INTO users (username, password, roles, active, timezone) "
            "VALUES (?, ?, ?, ?, ?)",
            ("admin", "x", json.dumps(["admin"]), 1, "America/Chicago"),
        )
        conn.commit()
        conn.close()

        command.downgrade(cfg, BEFORE_USER_TIMEZONE)

        conn = connect()
        conn.execute(
            "UPDATE users SET failed_login_count = failed_login_count + 1 "
            "WHERE username = 'admin'"
        )
        conn.commit()
        changed = conn.execute(
            "SELECT changed_columns FROM audit_log WHERE table_name='users' "
            "AND operation='U'"
        ).fetchone()[0]
        conn.close()
        assert json.loads(changed) == ["failed_login_count"]
