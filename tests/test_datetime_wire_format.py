"""Every datetime a response body carries serializes as RFC 3339 UTC with a
literal `Z`; every datetime a request body carries may arrive with any
offset, or none, and is normalized to naive UTC before it reaches a column.
See FEATURE_EXPANSION_PLAN.md section 2 and API contract 9.1."""

import base64
import re
from datetime import datetime, timedelta

from persistence.base import Clock
from persistence.user import User
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService
from tests.test_oauth import REDIRECT_URI

PDF_BASE64 = base64.b64encode(b"%PDF-1.4\n%fake pdf content for testing\n").decode()

Z_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def assert_z(value: str | None) -> None:
    assert value is not None
    assert Z_DATETIME.match(value), value


class TestApplicationAndEventFields:
    async def test_application_summary_fields(self, client, admin, application):
        assert_z(application["created_at"])
        assert_z(application["updated_at"])
        assert application["status_changed_at"] is None

        event = await client.post(
            f"/applications/{application['id']}/events",
            headers=admin.headers,
            json={"status": "screening"},
        )
        assert event.status_code == 201, event.text

        refreshed = await client.get(
            f"/applications/{application['id']}", headers=admin.headers
        )
        assert refreshed.status_code == 200, refreshed.text
        assert_z(refreshed.json()["status_changed_at"])

    async def test_event_write_response_fields(self, client, admin, application_event):
        assert_z(application_event["event"]["occurred_at"])
        assert_z(application_event["event"]["created_at"])
        assert_z(application_event["application_status_changed_at"])

    async def test_date_submitted_round_trips_bare(self, client, admin, company):
        response = await client.post(
            "/applications",
            headers=admin.headers,
            json={"company_id": company["id"], "date_submitted": "2026-06-01"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["date_submitted"] == "2026-06-01"


class TestCompanyFields:
    async def test_company_summary_fields(self, company):
        assert_z(company["created_at"])
        assert_z(company["updated_at"])

    async def test_relationship_and_stack_fields(self, client, admin, company):
        other = await client.post(
            "/companies", headers=admin.headers, json={"name": "Other Co"}
        )
        assert other.status_code == 201, other.text

        relationship = await client.post(
            f"/companies/{company['id']}/relationships",
            headers=admin.headers,
            json={"to_company_id": other.json()["id"], "type": "vendor_of"},
        )
        assert relationship.status_code == 201, relationship.text
        assert_z(relationship.json()["created_at"])

        stack_item = await client.post(
            f"/companies/{company['id']}/stack",
            headers=admin.headers,
            json={"name": "Python", "type": "programming_language"},
        )
        assert stack_item.status_code == 201, stack_item.text
        assert_z(stack_item.json()["created_at"])
        assert_z(stack_item.json()["updated_at"])


class TestContactFields:
    async def test_contact_fields(self, client, admin, company):
        response = await client.post(
            "/contacts",
            headers=admin.headers,
            json={"company_id": company["id"], "first_name": "Dana"},
        )
        assert response.status_code == 201, response.text
        assert_z(response.json()["created_at"])
        assert_z(response.json()["updated_at"])


class TestAttachmentFields:
    async def test_attachment_created_at(self, client, admin, application):
        response = await client.post(
            f"/applications/{application['id']}/attachments",
            headers=admin.headers,
            json={
                "kind": "resume",
                "filename": "resume.pdf",
                "content_type": "application/pdf",
                "content_base64": PDF_BASE64,
            },
        )
        assert response.status_code == 201, response.text
        assert_z(response.json()["created_at"])


class TestAuditFields:
    async def test_audit_changed_at(self, client, admin, company):
        response = await client.get(
            "/audit", headers=admin.headers, params={"table": "companies"}
        )
        assert response.status_code == 200, response.text
        rows = response.json()["data"]
        assert rows
        for row in rows:
            assert_z(row["changed_at"])


class TestApiKeyFields:
    async def test_api_key_created_at(self, client, admin):
        response = await client.post(
            "/api-keys",
            headers=admin.headers,
            json={"name": "a-key", "scopes": [Scopes.APPLICATIONS_READ.value]},
        )
        assert response.status_code == 200, response.text
        assert_z(response.json()["api_key"]["created_at"])
        assert response.json()["api_key"]["last_used_at"] is None
        assert response.json()["api_key"]["revoked_at"] is None


class TestMfaFields:
    async def test_mfa_credential_status_fields(self, client, enrolled):
        response = await client.get("/users/me/mfa", headers=enrolled.actor.headers)
        assert response.status_code == 200, response.text
        credential = response.json()["credentials"][0]
        assert_z(credential["created_at"])
        assert_z(credential["activated_at"])


class TestDocumentFields:
    async def test_get_and_list_document_created_at(self, client, admin, stored_resume):
        get_response = await client.get(
            "/documents/resume/resume.json", headers=admin.headers
        )
        assert get_response.status_code == 200, get_response.text
        assert_z(get_response.json()["data"][0]["created_at"])

        list_response = await client.get("/documents", headers=admin.headers)
        assert list_response.status_code == 200, list_response.text
        entries = [
            entry
            for entry in list_response.json()["data"]
            if entry["name"] == "resume.json"
        ]
        assert entries
        assert_z(entries[0]["created_at"])


class TestAdminUserFields:
    async def test_locked_until_and_locked_permanently_at(self, client, admin):
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, admin.user_id)
            assert user is not None
            user.locked_until = Clock.utcnow()
            user.locked_permanently_at = Clock.utcnow()
            await db.commit()

        response = await client.get("/users", headers=admin.headers)
        assert response.status_code == 200, response.text
        row = next(row for row in response.json()["data"] if row["id"] == admin.user_id)
        assert_z(row["locked_until"])
        assert_z(row["locked_permanently_at"])


class TestOAuthClientFields:
    async def test_admin_client_created_at(self, client, admin):
        response = await client.post(
            "/oauth-clients",
            headers=admin.headers,
            json={"client_name": "Test Client", "redirect_uris": [REDIRECT_URI]},
        )
        assert response.status_code == 200, response.text
        assert_z(response.json()["client"]["created_at"])


class TestRequestSideNormalization:
    async def test_offset_input_stores_the_equivalent_utc_instant(
        self, client, admin, application
    ):
        response = await client.post(
            f"/applications/{application['id']}/events",
            headers=admin.headers,
            json={"status": "screening", "occurred_at": "2026-01-01T14:00:00+02:00"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["event"]["occurred_at"] == "2026-01-01T12:00:00Z"

    async def test_naive_input_is_treated_as_utc(self, client, admin, application):
        response = await client.post(
            f"/applications/{application['id']}/events",
            headers=admin.headers,
            json={"status": "screening", "occurred_at": "2026-01-01T12:00:00"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["event"]["occurred_at"] == "2026-01-01T12:00:00Z"

    async def test_epoch_input_is_accepted_and_normalized(
        self, client, admin, application
    ):
        """Pydantic accepts a numeric epoch for a `datetime` field, so this
        has to keep working. It is a regression test for the ordering of
        `Utc.normalize_in`: as a *before* validator it was handed the raw
        `int`, tried to read it as an ISO string and raised `TypeError` -
        which is not one of the exceptions pydantic converts, so a perfectly
        parseable body came back as a 500.
        """
        response = await client.post(
            f"/applications/{application['id']}/events",
            headers=admin.headers,
            json={"status": "screening", "occurred_at": 1767268800},
        )
        assert response.status_code == 201, response.text
        assert response.json()["event"]["occurred_at"] == "2026-01-01T12:00:00Z"

    async def test_unparseable_input_is_422_not_500(self, client, admin, application):
        response = await client.post(
            f"/applications/{application['id']}/events",
            headers=admin.headers,
            json={"status": "screening", "occurred_at": "not-a-datetime"},
        )
        assert response.status_code == 422, response.text


class TestRecomputeStatusOrdersByTheCorrectInstant:
    """The regression that motivated this section: an event entered from an
    offset browser must not sort against a server-written event as if it
    happened at its own local wall-clock digits."""

    async def test_offset_event_does_not_falsely_outrank_a_later_server_event(
        self, client, admin, company
    ):
        app_response = await client.post(
            "/applications", headers=admin.headers, json={"company_id": company["id"]}
        )
        assert app_response.status_code == 201, app_response.text
        application_id = app_response.json()["id"]

        later = await client.post(
            f"/applications/{application_id}/events",
            headers=admin.headers,
            json={"status": "screening"},
        )
        assert later.status_code == 201, later.text
        later_occurred_at = datetime.fromisoformat(later.json()["event"]["occurred_at"])

        # An instant genuinely one hour before `later`, expressed in -05:00
        # wall-clock time (local = UTC - 5h). A serializer that stored the
        # raw digits as UTC instead of converting them would place this nine
        # hours *after* `later`, wrongly making it the latest event.
        earlier_utc = later_occurred_at - timedelta(hours=1)
        earlier_local = earlier_utc - timedelta(hours=5)
        earlier_iso = earlier_local.strftime("%Y-%m-%dT%H:%M:%S") + "-05:00"

        earlier = await client.post(
            f"/applications/{application_id}/events",
            headers=admin.headers,
            json={"status": "job_offered", "occurred_at": earlier_iso},
        )
        assert earlier.status_code == 201, earlier.text

        detail = await client.get(
            f"/applications/{application_id}", headers=admin.headers
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["status"] == "screening"
