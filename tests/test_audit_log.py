"""The audit log: trigger behaviour, actor attribution, GET /audit, and the
structural guard against a future secret leak."""

import base64

from sqlalchemy import select, text

from persistence.audit_log import AuditLogEntry
from persistence.base import SQAlchemyBase
from services.database.database_service import DatabaseService
from services.tracking.audit_columns import AuditColumns


class TestStructuralGuard:
    """Guards the design, not one run of it - see section 11.5."""

    def test_audited_columns_are_real_columns(self):
        tables_by_name = {
            table.name: table for table in SQAlchemyBase.metadata.tables.values()
        }
        for table_name, (_, _, columns) in AuditColumns.AUDITED.items():
            table = tables_by_name[table_name]
            real_columns = {column.name for column in table.columns}
            for column in columns:
                assert column in real_columns, f"{table_name}.{column} does not exist"

    def test_no_secret_column_is_ever_audited(self):
        for table_name, (_, _, columns) in AuditColumns.AUDITED.items():
            leaked = set(columns) & AuditColumns.SECRET_COLUMN_NAMES
            assert not leaked, f"{table_name} audits secret column(s): {leaked}"

    def test_pk_and_user_columns_are_included_in_audited_columns(self):
        for pk_col, user_col, columns in AuditColumns.AUDITED.values():
            assert pk_col in columns
            if user_col is not None:
                assert user_col in columns


class TestLiveTriggersMatchTheAllowlist:
    """The structural guard above checks the allowlist against the models.
    This checks the *triggers the migrations actually built* against the
    allowlist, which is the half that catches the failure the audit-log
    migration warns about: SQLite names its audited columns literally, so a
    migration that adds a column without recreating that table's triggers
    leaves them silently writing an out-of-date row.
    """

    @staticmethod
    async def _trigger_sql(table: str) -> str:
        async with DatabaseService.engine().begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                        "AND tbl_name = :table"
                    ),
                    {"table": table},
                )
            ).fetchall()
        return "\n".join(row[0] for row in rows if row[0])

    async def test_every_audited_column_appears_in_its_triggers(self):
        for table, (_pk, _user, columns) in AuditColumns.AUDITED.items():
            sql = await self._trigger_sql(table)
            assert sql, f"{table} has no audit triggers"
            for column in columns:
                assert f"'{column}'" in sql, (
                    f"{table}'s audit triggers do not record {column!r} — a "
                    "migration added the column without recreating them"
                )

    async def test_no_excluded_column_appears_in_its_triggers(self):
        for table, excluded in AuditColumns.EXCLUDED.items():
            sql = await self._trigger_sql(table)
            for column in excluded:
                assert f"'{column}'" not in sql, (
                    f"{table}'s audit triggers record {column!r}, which is "
                    "excluded for carrying a secret"
                )


class TestCompanyTriggers:
    async def test_insert_writes_an_i_row(self, client, admin):
        response = await client.post(
            "/companies", headers=admin.headers, json={"name": "Plaid"}
        )
        company_id = response.json()["id"]

        audit = await client.get(
            "/audit",
            headers=admin.headers,
            params={"table": "companies", "row_id": str(company_id)},
        )
        rows = audit.json()["data"]
        insert_rows = [row for row in rows if row["operation"] == "I"]
        assert len(insert_rows) == 1
        assert insert_rows[0]["row_user_id"] == admin.user_id
        assert insert_rows[0]["old_data"] is None
        assert insert_rows[0]["new_data"]["name"] == "Plaid"

    async def test_update_lists_exactly_the_changed_columns(
        self, client, admin, company
    ):
        await client.patch(
            f"/companies/{company['id']}",
            headers=admin.headers,
            json={"description": "A fintech company."},
        )
        audit = await client.get(
            "/audit",
            headers=admin.headers,
            params={"table": "companies", "row_id": str(company["id"])},
        )
        update_rows = [row for row in audit.json()["data"] if row["operation"] == "U"]
        assert len(update_rows) == 1
        assert update_rows[0]["changed_columns"] == ["description", "updated_at"]

    async def test_a_no_op_update_writes_nothing(self, client, admin, company):
        before = await client.get(
            "/audit",
            headers=admin.headers,
            params={"table": "companies", "row_id": str(company["id"])},
        )
        before_count = before.json()["total"]

        # Same name as it already has - the service short-circuits before
        # ever issuing an UPDATE, so there is nothing for the trigger to see.
        await client.patch(
            f"/companies/{company['id']}",
            headers=admin.headers,
            json={"name": company["name"]},
        )

        after = await client.get(
            "/audit",
            headers=admin.headers,
            params={"table": "companies", "row_id": str(company["id"])},
        )
        assert after.json()["total"] == before_count

    async def test_delete_writes_a_d_row_with_old_data(self, client, admin, company):
        await client.delete(f"/companies/{company['id']}", headers=admin.headers)
        audit = await client.get(
            "/audit",
            headers=admin.headers,
            params={"table": "companies", "row_id": str(company["id"])},
        )
        delete_rows = [row for row in audit.json()["data"] if row["operation"] == "D"]
        assert len(delete_rows) == 1
        assert delete_rows[0]["old_data"]["name"] == company["name"]
        assert delete_rows[0]["new_data"] is None


class TestActorAttribution:
    async def test_actor_matches_the_authenticated_caller(self, client, admin):
        response = await client.post(
            "/companies", headers=admin.headers, json={"name": "Plaid"}
        )
        company_id = response.json()["id"]

        audit = await client.get(
            "/audit",
            headers=admin.headers,
            params={"table": "companies", "row_id": str(company_id)},
        )
        row = audit.json()["data"][0]
        assert row["actor_user_id"] == admin.user_id
        assert row["actor_credential"] == "jwt"


class TestActorDoesNotLeakBetweenRequests:
    """On SQLite the actor is a row, not a transaction-local setting, so it
    outlives the transaction that set it. Every write to an audited table has
    to state its own actor or it inherits the last one bound — which files one
    user's change under another user's name. These are the two paths that
    write to an audited table without an authenticated principal.
    """

    @staticmethod
    async def _newest(db, table: str, row_pk: str):
        return (
            (
                await db.execute(
                    select(AuditLogEntry)
                    .where(
                        AuditLogEntry.table_name == table,
                        AuditLogEntry.row_pk == row_pk,
                        AuditLogEntry.operation == "U",
                    )
                    .order_by(AuditLogEntry.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

    async def test_a_failed_login_is_not_attributed_to_a_previous_caller(
        self, client, admin, member
    ):
        # Binds admin as the actor for the rest of this SQLite row's life.
        created = await client.post(
            "/companies", headers=admin.headers, json={"name": "Acme"}
        )
        assert created.status_code == 201, created.text

        failed = await client.post(
            "/token", data={"username": member.username, "password": "wrong-password"}
        )
        assert failed.status_code in (401, 429), failed.text

        async with DatabaseService.session() as db:
            row = await self._newest(db, "users", str(member.user_id))
        assert row is not None, "the failed login should have been audited"
        assert row.actor_user_id is None
        assert row.actor_credential is None

    async def test_api_key_use_is_not_attributed_to_a_previous_caller(
        self, client, admin, member
    ):
        minted = await client.post(
            "/api-keys",
            headers=member.headers,
            json={"name": "probe", "scopes": ["resume:read"]},
        )
        assert minted.status_code == 200, minted.text
        key_id = minted.json()["api_key"]["id"]

        created = await client.post(
            "/companies", headers=admin.headers, json={"name": "Globex"}
        )
        assert created.status_code == 201, created.text

        used = await client.get(
            "/documents", headers={"Authorization": f"Bearer {minted.json()['key']}"}
        )
        assert used.status_code == 200, used.text

        async with DatabaseService.session() as db:
            row = await self._newest(db, "api_keys", str(key_id))
        assert row is not None, "spending the key should have been audited"
        # The key's owner, not whoever last used the service.
        assert row.actor_user_id == member.user_id
        assert row.actor_credential == "api_key"


class TestAttachmentContentIsNeverAudited:
    async def test_content_base64_absent_from_new_data(
        self, client, admin, application
    ):
        pdf = base64.b64encode(b"%PDF-1.4\nfake\n").decode()
        created = await client.post(
            f"/applications/{application['id']}/attachments",
            headers=admin.headers,
            json={
                "kind": "resume",
                "filename": "r.pdf",
                "content_type": "application/pdf",
                "content_base64": pdf,
            },
        )
        attachment_id = created.json()["id"]

        audit = await client.get(
            "/audit",
            headers=admin.headers,
            params={"table": "application_attachments", "row_id": str(attachment_id)},
        )
        row = audit.json()["data"][0]
        assert "content_base64" not in row["new_data"]


class TestUserPasswordIsNeverAudited:
    async def test_a_combined_update_never_leaks_the_password(
        self, client, admin, password
    ):
        """A pure password-only change touches no audited column and so
        writes no row at all - see AuditColumns and the trigger's
        "nothing audited changed" short-circuit. Bundling it with a real,
        audited field change is what actually exercises the exclusion."""
        new_password = password + "New1!"
        response = await client.patch(
            f"/users/{admin.user_id}",
            headers=admin.headers,
            json={
                "first_name": "Renamed",
                "password": new_password,
                "password_retype": new_password,
            },
        )
        assert response.status_code == 204, response.text

        audit = await client.get(
            "/audit",
            headers=admin.headers,
            params={"table": "users", "row_id": str(admin.user_id)},
        )
        update_rows = [row for row in audit.json()["data"] if row["operation"] == "U"]
        assert len(update_rows) == 1
        assert "password" not in update_rows[0]["old_data"]
        assert "password" not in update_rows[0]["new_data"]
        assert "password" not in update_rows[0]["changed_columns"]
        assert "first_name" in update_rows[0]["changed_columns"]


class TestGetAudit:
    async def test_never_returns_another_users_rows(
        self, client, admin, other_owner, company
    ):
        response = await client.get(
            "/audit",
            headers=other_owner.headers,
            params={"table": "companies", "row_id": str(company["id"])},
        )
        assert response.json()["data"] == []
        assert response.json()["total"] == 0

    async def test_requires_the_audit_read_scope(self, client, admin):
        response = await client.get("/audit", headers=admin.headers)
        assert response.status_code == 200

    async def test_requires_a_credential(self, client):
        response = await client.get("/audit")
        assert response.status_code == 401

    async def test_unknown_table_is_422(self, client, admin):
        response = await client.get(
            "/audit", headers=admin.headers, params={"table": "not_a_real_table"}
        )
        assert response.status_code == 422

    async def test_ordered_newest_first(self, client, admin, company):
        await client.patch(
            f"/companies/{company['id']}",
            headers=admin.headers,
            json={"description": "first"},
        )
        await client.patch(
            f"/companies/{company['id']}",
            headers=admin.headers,
            json={"description": "second"},
        )
        response = await client.get(
            "/audit",
            headers=admin.headers,
            params={"table": "companies", "row_id": str(company["id"])},
        )
        changed_ats = [row["changed_at"] for row in response.json()["data"]]
        assert changed_ats == sorted(changed_ats, reverse=True)

    async def test_oauth_clients_are_never_visible_over_http(self, client, admin):
        """`oauth_clients` has no owning user, so `row_user_id` is always
        NULL on its audit rows - and the query always filters on the
        caller's own id, so those rows can never match."""
        response = await client.get(
            "/audit", headers=admin.headers, params={"table": "oauth_clients"}
        )
        assert response.json()["data"] == []
