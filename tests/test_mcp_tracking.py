"""The tracking MCP tools. Follows tests/test_mcp.py's monkeypatch pattern:
`TrackingTools.current_user_id` / `current_scopes` stand in for a real access
token, since there is no MCP transport in these tests."""

import json
from typing import get_args

import pytest
from fastmcp.exceptions import ToolError
from mcp.types import TextContent

from routers.mcp_tracking import (
    ApplicationStatusLiteral,
    StackItemInput,
    StackItemTypeLiteral,
    TrackingTools,
    add_application_event,
    add_company_stack_items,
    create_company,
    create_contact,
    get_application,
    get_company,
    record_application,
    search_applications,
    search_companies,
    search_contacts,
)
from services.auth.scopes import Scopes
from services.tracking.enums import ApplicationStatus, StackItemType


def _text(result) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


@pytest.fixture
def as_admin(monkeypatch, admin):
    monkeypatch.setattr(TrackingTools, "current_user_id", lambda: admin.user_id)
    monkeypatch.setattr(TrackingTools, "current_scopes", lambda: frozenset(Scopes))
    return admin


@pytest.fixture
def as_narrowed(monkeypatch, admin):
    def narrow(*scopes: Scopes):
        monkeypatch.setattr(TrackingTools, "current_user_id", lambda: admin.user_id)
        monkeypatch.setattr(TrackingTools, "current_scopes", lambda: frozenset(scopes))
        return admin

    return narrow


class TestLiteralsMatchEnums:
    """A hand-written Literal that drifts is worse than no constraint - see
    section 8.3 of the tracking plan."""

    def test_application_status(self):
        assert set(get_args(ApplicationStatusLiteral)) == {
            status.value for status in ApplicationStatus
        }

    def test_stack_item_type(self):
        assert set(get_args(StackItemTypeLiteral)) == {
            item_type.value for item_type in StackItemType
        }


class TestSearchCompanies:
    async def test_ranks_by_similarity(self, as_admin, company):
        result = await TrackingTools.search_companies("Acme")
        assert result["companies"][0]["id"] == company["id"]
        assert result["companies"][0]["match"] == "exact"

    async def test_no_match_is_an_empty_list(self, as_admin, company):
        result = await TrackingTools.search_companies("Totally Unrelated Corp")
        assert result["companies"] == []

    async def test_scoped_to_the_caller(self, as_admin, other_owner, client):
        await client.post(
            "/companies", headers=other_owner.headers, json={"name": "Acme Inc"}
        )
        result = await TrackingTools.search_companies("Acme")
        assert result["companies"] == []


class TestGetCompany:
    async def test_returns_detail(self, as_admin, company):
        result = await TrackingTools.get_company(company["id"])
        assert result["id"] == company["id"]
        assert result["stack"] == []

    async def test_unknown_id_raises_tool_error(self, as_admin):
        with pytest.raises(ToolError):
            await TrackingTools.get_company(999999)

    async def test_another_users_company_raises(self, as_admin, other_owner, client):
        created = await client.post(
            "/companies", headers=other_owner.headers, json={"name": "Theirs"}
        )
        with pytest.raises(ToolError):
            await TrackingTools.get_company(created.json()["id"])


class TestCreateCompany:
    async def test_happy_path(self, as_admin):
        result = await TrackingTools.create_company("Plaid")
        assert result["name"] == "Plaid"

    async def test_duplicate_is_refused_with_candidates(self, as_admin, company):
        with pytest.raises(ToolError, match="Acme Inc"):
            await TrackingTools.create_company("Acme Inc")

    async def test_confirm_overrides_a_fuzzy_duplicate(self, as_admin, company):
        with pytest.raises(ToolError):
            await TrackingTools.create_company("Acmee Inc")
        result = await TrackingTools.create_company(
            "Acmee Inc", confirm_create_duplicate=True
        )
        assert result["name"] == "Acmee Inc"


class TestAddCompanyStackItems:
    async def test_creates_and_skips_in_one_batch(self, as_admin, company, client):
        await client.post(
            f"/companies/{company['id']}/stack",
            headers=as_admin.headers,
            json={"name": "Go", "type": "programming_language"},
        )
        result = await TrackingTools.add_company_stack_items(
            company["id"],
            [
                StackItemInput(name="Go", type="programming_language"),
                StackItemInput(name="Rust", type="programming_language"),
            ],
        )
        assert len(result["created"]) == 1
        assert result["created"][0]["name"] == "Rust"
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["name"] == "Go"


class TestSearchContacts:
    async def test_happy_path(self, as_admin, client):
        await client.post(
            "/contacts", headers=as_admin.headers, json={"first_name": "Sam"}
        )
        result = await TrackingTools.search_contacts(query="Sam")
        assert len(result["data"]) == 1


class TestCreateContact:
    async def test_happy_path(self, as_admin):
        result = await TrackingTools.create_contact(first_name="Sam")
        assert result["first_name"] == "Sam"

    async def test_needs_identity(self, as_admin):
        with pytest.raises(ToolError):
            await TrackingTools.create_contact()

    async def test_duplicate_email_is_refused_then_confirmable(self, as_admin):
        await TrackingTools.create_contact(email="sam@example.com")
        with pytest.raises(ToolError):
            await TrackingTools.create_contact(email="sam@example.com")
        result = await TrackingTools.create_contact(
            email="sam@example.com", confirm_create_duplicate=True
        )
        assert result["email"] == "sam@example.com"


class TestSearchAndGetApplications:
    async def test_search_returns_summaries_only(self, as_admin, application):
        result = await TrackingTools.search_applications(query="engineer")
        assert len(result["data"]) == 1
        assert "job_description" not in result["data"][0]

    async def test_get_returns_full_detail(self, as_admin, application):
        result = await TrackingTools.get_application(application["id"])
        assert "events" in result
        assert "attachments" in result

    async def test_status_filter(self, as_admin, application):
        result = await TrackingTools.search_applications(status="rejected")
        assert result["data"] == []
        result = await TrackingTools.search_applications(status="submitted")
        assert len(result["data"]) == 1


class TestRecordApplication:
    async def test_requires_exactly_one_of_company_id_or_name(self, as_admin, company):
        with pytest.raises(ToolError):
            await TrackingTools.record_application()
        with pytest.raises(ToolError):
            await TrackingTools.record_application(
                company_id=company["id"], company_name="Acme Inc"
            )

    async def test_with_existing_company_id(self, as_admin, company):
        result = await TrackingTools.record_application(
            company_id=company["id"], job_title="Engineer"
        )
        assert result["company_id"] == company["id"]
        assert result["company_created"] is False
        assert result["status"] == "submitted"

    async def test_with_new_company_name(self, as_narrowed):
        as_narrowed(Scopes.APPLICATIONS_WRITE, Scopes.COMPANIES_WRITE)
        result = await TrackingTools.record_application(
            company_name="Plaid", job_title="Engineer"
        )
        assert result["company_created"] is True

    async def test_exact_normalized_company_match_is_reused_silently(
        self, as_admin, company
    ):
        result = await TrackingTools.record_application(
            company_name="Acme Inc", job_title="Engineer"
        )
        assert result["company_id"] == company["id"]
        assert result["company_created"] is False

    async def test_new_company_requires_companies_write(self, as_narrowed):
        as_narrowed(Scopes.APPLICATIONS_WRITE)
        with pytest.raises(ToolError, match="companies:write"):
            await TrackingTools.record_application(
                company_name="Brand New Co", job_title="Engineer"
            )

    async def test_duplicate_application_is_refused_then_confirmable(
        self, as_admin, company
    ):
        await TrackingTools.record_application(
            company_id=company["id"],
            job_title="Engineer",
            date_submitted="2026-06-01",
        )
        with pytest.raises(ToolError):
            await TrackingTools.record_application(
                company_id=company["id"],
                job_title="Engineer",
                date_submitted="2026-06-01",
            )
        result = await TrackingTools.record_application(
            company_id=company["id"],
            job_title="Engineer",
            date_submitted="2026-06-01",
            confirm_create_duplicate=True,
        )
        assert result["company_id"] == company["id"]


class TestAddApplicationEvent:
    async def test_happy_path(self, as_admin, application):
        result = await TrackingTools.add_application_event(
            application["id"], status="rejected"
        )
        assert result["application_status"] == "rejected"
        assert result["event"]["status"] == "rejected"

    async def test_needs_content(self, as_admin, application):
        with pytest.raises(ToolError):
            await TrackingTools.add_application_event(application["id"])

    async def test_unknown_contact_id_raises(self, as_admin, application):
        with pytest.raises(ToolError):
            await TrackingTools.add_application_event(
                application["id"], description="note", contact_id=999999
            )


class TestToolAdaptersReturnASingleTextBlock:
    """The registered `@tool` functions themselves, not just the `TrackingTools`
    classmethods they delegate to - same shape as test_mcp.py's
    TestNoOutputSchema, extended to every tracking tool."""

    async def test_search_companies(self, as_admin, company):
        result = await search_companies("Acme")
        assert len(result.content) == 1
        assert json.loads(_text(result))["companies"][0]["id"] == company["id"]

    async def test_get_company(self, as_admin, company):
        result = await get_company(company["id"])
        assert json.loads(_text(result))["id"] == company["id"]

    async def test_create_company(self, as_admin):
        result = await create_company("Plaid")
        assert json.loads(_text(result))["name"] == "Plaid"

    async def test_add_company_stack_items(self, as_admin, company):
        result = await add_company_stack_items(
            company["id"], [StackItemInput(name="Go", type="programming_language")]
        )
        assert len(json.loads(_text(result))["created"]) == 1

    async def test_search_contacts(self, as_admin):
        await TrackingTools.create_contact(first_name="Sam")
        result = await search_contacts(query="Sam")
        assert len(json.loads(_text(result))["data"]) == 1

    async def test_create_contact(self, as_admin):
        result = await create_contact(first_name="Sam")
        assert json.loads(_text(result))["first_name"] == "Sam"

    async def test_search_applications(self, as_admin, application):
        result = await search_applications(query="engineer")
        assert len(json.loads(_text(result))["data"]) == 1

    async def test_get_application(self, as_admin, application):
        result = await get_application(application["id"])
        assert json.loads(_text(result))["id"] == application["id"]

    async def test_record_application(self, as_admin, company):
        result = await record_application(
            company_id=company["id"], job_title="Engineer"
        )
        assert json.loads(_text(result))["company_id"] == company["id"]

    async def test_add_application_event(self, as_admin, application):
        result = await add_application_event(application["id"], status="rejected")
        assert json.loads(_text(result))["application_status"] == "rejected"
