"""The tracking MCP tools. Follows tests/test_mcp.py's monkeypatch pattern:
`current_user_id` / `current_scopes` stand in for a real access token, since
there is no MCP transport in these tests. They are patched on `McpToolBase`
rather than on `TrackingTools`, so that `ConfirmTools` — a sibling subclass,
not a child of this one — reads the same stub credential.

Every write tool is a preview/confirm pair (see MCP_WRITEBACK_PLAN.md §3):
`preview_*` resolves duplicates and returns a `confirm_token`, and only
`confirm` writes anything. See `TestConfirmDispatch`."""

import json
import re
from pathlib import Path
from typing import get_args

import pytest
from fastmcp.exceptions import ToolError
from mcp.types import TextContent

from routers import mcp_documents, mcp_tracking
from routers.mcp_confirm import ConfirmTools, confirm
from routers.mcp_tracking import (
    ApplicationStatusLiteral,
    AttachmentKindLiteral,
    CompanyRelationshipTypeLiteral,
    StackItemInput,
    StackItemTypeLiteral,
    TrackingTools,
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
from services.auth.mcp_tools import McpToolBase
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


async def _confirm(confirm_token: str) -> dict:
    """One `ConfirmTools.confirm` call, unwrapped to the committed write's own
    result — these tests assert on what was written, not on the envelope
    naming which tool wrote it. `TestConfirmDispatch` covers the envelope."""
    return (await ConfirmTools.confirm(confirm_token))["result"]


@pytest.fixture
def as_admin(monkeypatch, admin):
    monkeypatch.setattr(McpToolBase, "current_user_id", lambda: admin.user_id)
    monkeypatch.setattr(McpToolBase, "current_scopes", lambda: frozenset(Scopes))
    return admin


@pytest.fixture
def as_narrowed(monkeypatch, admin):
    def narrow(*scopes: Scopes):
        monkeypatch.setattr(McpToolBase, "current_user_id", lambda: admin.user_id)
        monkeypatch.setattr(McpToolBase, "current_scopes", lambda: frozenset(scopes))
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
        result = await _confirm(preview["confirm_token"])
        assert result["name"] == "Plaid"

    async def test_confirming_twice_is_refused(self, as_admin):
        preview = await TrackingTools.preview_create_company("Plaid")
        await _confirm(preview["confirm_token"])
        with pytest.raises(ToolError):
            await _confirm(preview["confirm_token"])

    async def test_confirm_with_no_prior_preview_is_refused(self, as_admin):
        with pytest.raises(ToolError):
            await _confirm("not-a-real-token")

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
        result = await _confirm(preview["confirm_token"])
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

        result = await _confirm(preview["confirm_token"])
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
    return await _confirm(preview["confirm_token"])


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
    return await _confirm(preview["confirm_token"])


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
        result = await _confirm(preview["confirm_token"])
        assert result["company_created"] is True

    async def test_exact_normalized_company_match_is_reused_silently(
        self, as_admin, company
    ):
        preview = await TrackingTools.preview_record_application(
            company_name="Acme Inc", job_title="Engineer"
        )
        assert preview["preview"]["company"]["action"] == "reuse_existing"
        result = await _confirm(preview["confirm_token"])
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
        result = await _confirm(preview["confirm_token"])
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
        result = await _confirm(preview["confirm_token"])
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
        await _confirm(preview["confirm_token"])
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
        result = await _confirm(preview["confirm_token"])
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
        await _confirm(preview["confirm_token"])
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
        result = await confirm(confirm_token)
        assert json.loads(_text(result))["result"]["name"] == "Plaid"

    async def test_add_company_stack_items(self, as_admin, company):
        preview_result = await preview_add_company_stack_items(
            company["id"], [StackItemInput(name="Go", type="programming_language")]
        )
        confirm_token = json.loads(_text(preview_result))["confirm_token"]
        result = await confirm(confirm_token)
        assert len(json.loads(_text(result))["result"]["created"]) == 1

    async def test_search_contacts(self, as_admin):
        await _create_contact(first_name="Sam")
        result = await search_contacts(query="Sam")
        assert len(json.loads(_text(result))["data"]) == 1

    async def test_create_contact(self, as_admin):
        preview_result = await preview_create_contact(first_name="Sam")
        confirm_token = json.loads(_text(preview_result))["confirm_token"]
        result = await confirm(confirm_token)
        assert json.loads(_text(result))["result"]["first_name"] == "Sam"

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
        result = await confirm(confirm_token)
        assert json.loads(_text(result))["result"]["company_id"] == company["id"]

    async def test_add_application_event(self, as_admin, application):
        preview_result = await preview_add_application_event(
            application["id"], status="rejected"
        )
        confirm_token = json.loads(_text(preview_result))["confirm_token"]
        result = await confirm(confirm_token)
        assert json.loads(_text(result))["result"]["application_status"] == "rejected"

    async def test_create_company_relationship(self, as_admin, company, client):
        other = await client.post(
            "/companies", headers=as_admin.headers, json={"name": "Globex"}
        )
        preview_result = await preview_create_company_relationship(
            company["id"], other.json()["id"], "partner_of"
        )
        confirm_token = json.loads(_text(preview_result))["confirm_token"]
        result = await confirm(confirm_token)
        assert json.loads(_text(result))["result"]["from_company_id"] == company["id"]

    async def test_add_attachment(self, as_admin, application):
        preview_result = await preview_add_attachment(
            application["id"],
            "resume",
            "resume.pdf",
            "application/pdf",
            "JVBERi0xLjQKJeLjz9M=",
        )
        confirm_token = json.loads(_text(preview_result))["confirm_token"]
        result = await confirm(confirm_token)
        assert json.loads(_text(result))["result"]["filename"] == "resume.pdf"


class TestConfirmDispatch:
    """The one `confirm` tool that replaced the eight `confirm_*` ones.

    Collapsing them moved the scope check off the `@tool` decorator, which
    could not see which write a token stood for, and into
    `ConfirmationService.redeem`, which can. These tests are that check:
    a credential narrowed to one resource must not be able to commit
    another resource's previewed write, and must not lose its token
    finding that out.
    """

    async def test_names_the_tool_it_committed(self, as_admin):
        preview = await TrackingTools.preview_create_company("Plaid")
        result = await ConfirmTools.confirm(preview["confirm_token"])
        assert result["confirmed"] == "create_company"
        assert result["result"]["name"] == "Plaid"

    async def test_dispatches_on_the_token_not_on_the_caller(
        self, as_admin, application
    ):
        """Two different pending writes, confirmed through the same tool with
        nothing to distinguish them but their tokens."""
        company_preview = await TrackingTools.preview_create_company("Plaid")
        event_preview = await TrackingTools.preview_add_application_event(
            application["id"], status="rejected"
        )
        company = await ConfirmTools.confirm(company_preview["confirm_token"])
        event = await ConfirmTools.confirm(event_preview["confirm_token"])
        assert company["confirmed"] == "create_company"
        assert event["confirmed"] == "add_application_event"
        assert event["result"]["application_status"] == "rejected"

    async def test_wrong_resource_scope_is_refused(self, as_admin, as_narrowed):
        """`applications:write` alone must not commit a company write. The
        preview is taken with full scopes so that what is being tested is the
        confirm, not the preview's own gate."""
        preview = await TrackingTools.preview_create_company("Plaid")
        as_narrowed(Scopes.APPLICATIONS_READ, Scopes.APPLICATIONS_WRITE)
        with pytest.raises(ToolError, match="companies:write"):
            await ConfirmTools.confirm(preview["confirm_token"])

    async def test_a_refused_confirm_does_not_consume_the_token(
        self, as_admin, as_narrowed
    ):
        """Refusal happens before `consumed_at` is set, so the write is still
        committable once the caller has the scope for it — a burned token
        would turn a permissions mistake into lost work."""
        preview = await TrackingTools.preview_create_company("Plaid")
        as_narrowed(Scopes.APPLICATIONS_WRITE)
        with pytest.raises(ToolError):
            await ConfirmTools.confirm(preview["confirm_token"])

        as_narrowed(Scopes.COMPANIES_READ, Scopes.COMPANIES_WRITE)
        result = await ConfirmTools.confirm(preview["confirm_token"])
        assert result["result"]["name"] == "Plaid"

    async def test_an_unknown_pending_write_is_refused(self, as_admin, monkeypatch):
        """A token issued for a tool this build no longer registers — a
        retired tool, or one a newer build wrote. Refused rather than
        guessed at."""
        preview = await TrackingTools.preview_create_company("Plaid")
        monkeypatch.delitem(ConfirmTools.REGISTRY, "create_company")
        with pytest.raises(ToolError, match="does not know how to commit"):
            await ConfirmTools.confirm(preview["confirm_token"])

    async def test_every_preview_tool_has_a_registry_entry(self):
        """The registry is the only thing routing a token to its write. A
        `preview_*` whose tool name is missing from it issues tokens that
        can never be redeemed."""
        issued = set()
        for module in (mcp_tracking.__file__, mcp_documents.__file__):
            issued |= set(
                re.findall(r'issue_preview\(\s*"(\w+)"', Path(module).read_text())
            )
        assert issued == set(ConfirmTools.REGISTRY)
