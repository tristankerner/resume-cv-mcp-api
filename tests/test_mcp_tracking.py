"""The tracking MCP tools. Follows tests/test_mcp.py's monkeypatch pattern:
`TrackingTools.current_user_id` / `current_scopes` stand in for a real access
token, since there is no MCP transport in these tests.

Every write tool is a preview/confirm pair (see MCP_WRITEBACK_PLAN.md §3):
`preview_*` resolves duplicates and returns a `confirm_token`, and only
`confirm_*` writes anything."""

import json
from typing import get_args

import pytest
from fastmcp.exceptions import ToolError
from mcp.types import TextContent

from routers.mcp_tracking import (
    ApplicationStatusLiteral,
    AttachmentKindLiteral,
    CompanyRelationshipTypeLiteral,
    StackItemInput,
    StackItemTypeLiteral,
    TrackingTools,
    confirm_add_application_event,
    confirm_add_attachment,
    confirm_add_company_stack_items,
    confirm_create_company,
    confirm_create_company_relationship,
    confirm_create_contact,
    confirm_record_application,
    get_application,
    get_company,
    preview_add_application_event,
    preview_add_attachment,
    preview_add_company_stack_items,
    preview_create_company,
    preview_create_company_relationship,
    preview_create_contact,
    preview_record_application,
    search_applications,
    search_companies,
    search_contacts,
)
from services.auth.scopes import Scopes
from services.tracking.enums import (
    ApplicationStatus,
    AttachmentKind,
    CompanyRelationshipType,
    StackItemType,
)


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

    def test_company_relationship_type(self):
        assert set(get_args(CompanyRelationshipTypeLiteral)) == {
            relationship_type.value for relationship_type in CompanyRelationshipType
        }

    def test_attachment_kind(self):
        assert set(get_args(AttachmentKindLiteral)) == {
            kind.value for kind in AttachmentKind
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
    async def test_preview_returns_a_confirm_token(self, as_admin):
        preview = await TrackingTools.preview_create_company("Plaid")
        assert preview["preview"]["would_create"]["name"] == "Plaid"
        assert preview["confirm_token"]

    async def test_confirm_writes_the_company(self, as_admin):
        preview = await TrackingTools.preview_create_company("Plaid")
        result = await TrackingTools.confirm_create_company(preview["confirm_token"])
        assert result["name"] == "Plaid"

    async def test_confirming_twice_is_refused(self, as_admin):
        preview = await TrackingTools.preview_create_company("Plaid")
        await TrackingTools.confirm_create_company(preview["confirm_token"])
        with pytest.raises(ToolError):
            await TrackingTools.confirm_create_company(preview["confirm_token"])

    async def test_confirm_with_no_prior_preview_is_refused(self, as_admin):
        with pytest.raises(ToolError):
            await TrackingTools.confirm_create_company("not-a-real-token")

    async def test_duplicate_is_refused_with_candidates_at_preview_time(
        self, as_admin, company
    ):
        with pytest.raises(ToolError, match="Acme Inc"):
            await TrackingTools.preview_create_company("Acme Inc")

    async def test_confirm_flag_overrides_a_fuzzy_duplicate(self, as_admin, company):
        with pytest.raises(ToolError):
            await TrackingTools.preview_create_company("Acmee Inc")
        preview = await TrackingTools.preview_create_company(
            "Acmee Inc", confirm_create_duplicate=True
        )
        result = await TrackingTools.confirm_create_company(preview["confirm_token"])
        assert result["name"] == "Acmee Inc"


class TestAddCompanyStackItems:
    async def test_creates_and_skips_in_one_batch(self, as_admin, company, client):
        await client.post(
            f"/companies/{company['id']}/stack",
            headers=as_admin.headers,
            json={"name": "Go", "type": "programming_language"},
        )
        preview = await TrackingTools.preview_add_company_stack_items(
            company["id"],
            [
                StackItemInput(name="Go", type="programming_language"),
                StackItemInput(name="Rust", type="programming_language"),
            ],
        )
        assert len(preview["preview"]["would_create"]) == 1
        assert preview["preview"]["would_create"][0]["name"] == "Rust"
        assert len(preview["preview"]["would_skip"]) == 1
        assert preview["preview"]["would_skip"][0]["name"] == "Go"

        result = await TrackingTools.confirm_add_company_stack_items(
            preview["confirm_token"]
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


async def _create_contact(**kwargs) -> dict:
    """Preview then confirm, for tests that only care about the end state."""
    preview = await TrackingTools.preview_create_contact(**kwargs)
    return await TrackingTools.confirm_create_contact(preview["confirm_token"])


class TestCreateContact:
    async def test_happy_path(self, as_admin):
        result = await _create_contact(first_name="Sam")
        assert result["first_name"] == "Sam"

    async def test_needs_identity(self, as_admin):
        with pytest.raises(ToolError):
            await TrackingTools.preview_create_contact()

    async def test_duplicate_email_is_refused_then_confirmable(self, as_admin):
        await _create_contact(email="sam@example.com")
        with pytest.raises(ToolError):
            await TrackingTools.preview_create_contact(email="sam@example.com")
        result = await _create_contact(
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


async def _record_application(**kwargs) -> dict:
    preview = await TrackingTools.preview_record_application(**kwargs)
    return await TrackingTools.confirm_record_application(preview["confirm_token"])


class TestRecordApplication:
    async def test_requires_exactly_one_of_company_id_or_name(self, as_admin, company):
        with pytest.raises(ToolError):
            await TrackingTools.preview_record_application()
        with pytest.raises(ToolError):
            await TrackingTools.preview_record_application(
                company_id=company["id"], company_name="Acme Inc"
            )

    async def test_with_existing_company_id(self, as_admin, company):
        result = await _record_application(
            company_id=company["id"], job_title="Engineer"
        )
        assert result["company_id"] == company["id"]
        assert result["company_created"] is False
        assert result["status"] == "submitted"

    async def test_with_new_company_name(self, as_narrowed):
        as_narrowed(
            Scopes.APPLICATIONS_READ,
            Scopes.APPLICATIONS_WRITE,
            Scopes.COMPANIES_READ,
            Scopes.COMPANIES_WRITE,
        )
        preview = await TrackingTools.preview_record_application(
            company_name="Plaid", job_title="Engineer"
        )
        assert preview["preview"]["company"]["action"] == "create"
        result = await TrackingTools.confirm_record_application(
            preview["confirm_token"]
        )
        assert result["company_created"] is True

    async def test_exact_normalized_company_match_is_reused_silently(
        self, as_admin, company
    ):
        preview = await TrackingTools.preview_record_application(
            company_name="Acme Inc", job_title="Engineer"
        )
        assert preview["preview"]["company"]["action"] == "reuse_existing"
        result = await TrackingTools.confirm_record_application(
            preview["confirm_token"]
        )
        assert result["company_id"] == company["id"]
        assert result["company_created"] is False

    async def test_new_company_requires_companies_write(self, as_narrowed):
        as_narrowed(Scopes.APPLICATIONS_READ, Scopes.APPLICATIONS_WRITE)
        with pytest.raises(ToolError, match="companies:write"):
            await TrackingTools.preview_record_application(
                company_name="Brand New Co", job_title="Engineer"
            )

    async def test_duplicate_application_is_refused_then_confirmable(
        self, as_admin, company
    ):
        await _record_application(
            company_id=company["id"],
            job_title="Engineer",
            date_submitted="2026-06-01",
        )
        with pytest.raises(ToolError):
            await TrackingTools.preview_record_application(
                company_id=company["id"],
                job_title="Engineer",
                date_submitted="2026-06-01",
            )
        result = await _record_application(
            company_id=company["id"],
            job_title="Engineer",
            date_submitted="2026-06-01",
            confirm_create_duplicate=True,
        )
        assert result["company_id"] == company["id"]


class TestAddApplicationEvent:
    async def test_happy_path(self, as_admin, application):
        preview = await TrackingTools.preview_add_application_event(
            application["id"], status="rejected"
        )
        result = await TrackingTools.confirm_add_application_event(
            preview["confirm_token"]
        )
        assert result["application_status"] == "rejected"
        assert result["event"]["status"] == "rejected"

    async def test_needs_content(self, as_admin, application):
        with pytest.raises(ToolError):
            await TrackingTools.preview_add_application_event(application["id"])

    async def test_unknown_contact_id_raises(self, as_admin, application):
        with pytest.raises(ToolError):
            await TrackingTools.preview_add_application_event(
                application["id"], description="note", contact_id=999999
            )


class TestCompanyRelationships:
    @pytest.fixture
    async def other_company(self, client, as_admin) -> dict:
        response = await client.post(
            "/companies", headers=as_admin.headers, json={"name": "Globex"}
        )
        assert response.status_code == 201, response.text
        return response.json()

    async def test_search_returns_nothing_initially(self, as_admin, company):
        result = await TrackingTools.search_company_relationships(company["id"])
        assert result["relationships"] == []

    async def test_preview_reads_back_as_a_sentence(
        self, as_admin, company, other_company
    ):
        preview = await TrackingTools.preview_create_company_relationship(
            company["id"], other_company["id"], "staffing_agency_for"
        )
        assert company["name"] in preview["preview"]["sentence"]
        assert other_company["name"] in preview["preview"]["sentence"]

    async def test_confirm_writes_the_relationship(
        self, as_admin, company, other_company
    ):
        preview = await TrackingTools.preview_create_company_relationship(
            company["id"], other_company["id"], "staffing_agency_for"
        )
        result = await TrackingTools.confirm_create_company_relationship(
            preview["confirm_token"]
        )
        assert result["from_company_id"] == company["id"]
        assert result["to_company_id"] == other_company["id"]

        search = await TrackingTools.search_company_relationships(company["id"])
        assert len(search["relationships"]) == 1

    async def test_repeat_is_refused_at_preview_time(
        self, as_admin, company, other_company
    ):
        preview = await TrackingTools.preview_create_company_relationship(
            company["id"], other_company["id"], "staffing_agency_for"
        )
        await TrackingTools.confirm_create_company_relationship(
            preview["confirm_token"]
        )
        with pytest.raises(ToolError):
            await TrackingTools.preview_create_company_relationship(
                company["id"], other_company["id"], "staffing_agency_for"
            )

    async def test_self_relationship_is_refused(self, as_admin, company):
        with pytest.raises(ToolError):
            await TrackingTools.preview_create_company_relationship(
                company["id"], company["id"], "partner_of"
            )


class TestAddAttachment:
    PDF_BASE64 = "JVBERi0xLjQKJeLjz9M="  # "%PDF-1.4\n%..." — starts with %PDF-

    async def test_preview_returns_metadata_without_content(
        self, as_admin, application
    ):
        preview = await TrackingTools.preview_add_attachment(
            application["id"],
            "resume",
            "resume.pdf",
            "application/pdf",
            self.PDF_BASE64,
        )
        assert preview["preview"]["filename"] == "resume.pdf"
        assert preview["preview"]["sha256"]
        assert "content_base64" not in preview["preview"]

    async def test_confirm_writes_the_attachment(self, as_admin, application):
        preview = await TrackingTools.preview_add_attachment(
            application["id"],
            "resume",
            "resume.pdf",
            "application/pdf",
            self.PDF_BASE64,
        )
        result = await TrackingTools.confirm_add_attachment(preview["confirm_token"])
        assert result["filename"] == "resume.pdf"
        assert result["application_id"] == application["id"]

    async def test_duplicate_bytes_are_refused_at_preview_time(
        self, as_admin, application
    ):
        preview = await TrackingTools.preview_add_attachment(
            application["id"],
            "resume",
            "resume.pdf",
            "application/pdf",
            self.PDF_BASE64,
        )
        await TrackingTools.confirm_add_attachment(preview["confirm_token"])
        with pytest.raises(ToolError):
            await TrackingTools.preview_add_attachment(
                application["id"],
                "resume",
                "resume-again.pdf",
                "application/pdf",
                self.PDF_BASE64,
            )

    async def test_wrong_magic_bytes_are_refused_at_preview_time(
        self, as_admin, application
    ):
        with pytest.raises(ToolError):
            await TrackingTools.preview_add_attachment(
                application["id"],
                "resume",
                "resume.pdf",
                "application/pdf",
                "bm90IGEgcGRm",  # "not a pdf"
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
        preview_result = await preview_create_company("Plaid")
        confirm_token = json.loads(_text(preview_result))["confirm_token"]
        result = await confirm_create_company(confirm_token)
        assert json.loads(_text(result))["name"] == "Plaid"

    async def test_add_company_stack_items(self, as_admin, company):
        preview_result = await preview_add_company_stack_items(
            company["id"], [StackItemInput(name="Go", type="programming_language")]
        )
        confirm_token = json.loads(_text(preview_result))["confirm_token"]
        result = await confirm_add_company_stack_items(confirm_token)
        assert len(json.loads(_text(result))["created"]) == 1

    async def test_search_contacts(self, as_admin):
        await _create_contact(first_name="Sam")
        result = await search_contacts(query="Sam")
        assert len(json.loads(_text(result))["data"]) == 1

    async def test_create_contact(self, as_admin):
        preview_result = await preview_create_contact(first_name="Sam")
        confirm_token = json.loads(_text(preview_result))["confirm_token"]
        result = await confirm_create_contact(confirm_token)
        assert json.loads(_text(result))["first_name"] == "Sam"

    async def test_search_applications(self, as_admin, application):
        result = await search_applications(query="engineer")
        assert len(json.loads(_text(result))["data"]) == 1

    async def test_get_application(self, as_admin, application):
        result = await get_application(application["id"])
        assert json.loads(_text(result))["id"] == application["id"]

    async def test_record_application(self, as_admin, company):
        preview_result = await preview_record_application(
            company_id=company["id"], job_title="Engineer"
        )
        confirm_token = json.loads(_text(preview_result))["confirm_token"]
        result = await confirm_record_application(confirm_token)
        assert json.loads(_text(result))["company_id"] == company["id"]

    async def test_add_application_event(self, as_admin, application):
        preview_result = await preview_add_application_event(
            application["id"], status="rejected"
        )
        confirm_token = json.loads(_text(preview_result))["confirm_token"]
        result = await confirm_add_application_event(confirm_token)
        assert json.loads(_text(result))["application_status"] == "rejected"

    async def test_create_company_relationship(self, as_admin, company, client):
        other = await client.post(
            "/companies", headers=as_admin.headers, json={"name": "Globex"}
        )
        preview_result = await preview_create_company_relationship(
            company["id"], other.json()["id"], "partner_of"
        )
        confirm_token = json.loads(_text(preview_result))["confirm_token"]
        result = await confirm_create_company_relationship(confirm_token)
        assert json.loads(_text(result))["from_company_id"] == company["id"]

    async def test_add_attachment(self, as_admin, application):
        preview_result = await preview_add_attachment(
            application["id"],
            "resume",
            "resume.pdf",
            "application/pdf",
            "JVBERi0xLjQKJeLjz9M=",
        )
        confirm_token = json.loads(_text(preview_result))["confirm_token"]
        result = await confirm_add_attachment(confirm_token)
        assert json.loads(_text(result))["filename"] == "resume.pdf"
