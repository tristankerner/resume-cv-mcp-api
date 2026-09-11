"""The MCP surface shares AuthService.authenticate, so both credential types
work there without MCP-specific auth code. These tests hold that seam."""

import json
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool
from mcp.types import TextContent

from persistence.document import Document
from routers.mcp import ResumeTools, list_resume_documents, retrieve_resume_data
from services.auth.mcp_tools import McpToolBase
from services.auth.mcp_verifier import McpTokenVerifier
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService
from services.document.document_reader import DocumentReader
from services.document.document_types import DocumentType
from services.document.dtos.resume_object import ResumeMetadata, ResumePrivate
from services.document.dtos.resume_skill import ResumeSkill


@pytest.fixture
def verifier():
    return McpTokenVerifier()


async def mint(client, actor, scopes):
    response = await client.post(
        "/api-keys",
        headers=actor.headers,
        json={"name": "mcp", "scopes": [s.value for s in scopes]},
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestTokenVerifier:
    async def test_accepts_a_jwt(self, verifier, admin):
        token = await verifier.verify_token(admin.token)
        assert token is not None
        assert token.subject == admin.username
        assert token.client_id == admin.username

    async def test_jwt_carries_every_scope_the_role_grants(self, verifier, admin):
        token = await verifier.verify_token(admin.token)
        assert sorted(token.scopes) == sorted(s.value for s in Scopes)

    async def test_claims_carry_the_user_id(self, verifier, admin):
        token = await verifier.verify_token(admin.token)
        assert token.claims["user_id"] == admin.user_id

    async def test_api_key_claims_carry_the_owners_user_id(
        self, verifier, client, admin
    ):
        created = await mint(client, admin, [Scopes.RESUME_READ])
        token = await verifier.verify_token(created["key"])
        assert token.claims["user_id"] == admin.user_id

    async def test_accepts_an_api_key(self, verifier, client, admin):
        created = await mint(client, admin, [Scopes.RESUME_READ])
        token = await verifier.verify_token(created["key"])
        assert token is not None
        assert token.subject == admin.username

    async def test_api_key_scopes_are_narrowed(self, verifier, client, admin):
        created = await mint(client, admin, [Scopes.SKILL_READ])
        token = await verifier.verify_token(created["key"])
        assert token.scopes == [Scopes.SKILL_READ.value]

    async def test_claims_do_not_leak_the_credential(self, verifier, client, admin):
        created = await mint(client, admin, [Scopes.RESUME_READ])
        token = await verifier.verify_token(created["key"])
        assert created["key"] not in str(token.claims)

    async def test_expiry_is_reported_for_a_jwt(self, verifier, admin):
        assert (await verifier.verify_token(admin.token)).expires_at is not None

    async def test_no_expiry_for_a_non_expiring_key(self, verifier, client, admin):
        created = await mint(client, admin, [Scopes.RESUME_READ])
        assert (await verifier.verify_token(created["key"])).expires_at is None

    @pytest.mark.parametrize("token", ["nonsense", "rsm_short", ""])
    async def test_rejects_bad_tokens(self, verifier, token):
        assert await verifier.verify_token(token) is None

    async def test_rejects_a_revoked_key(self, verifier, client, admin):
        created = await mint(client, admin, [Scopes.RESUME_READ])
        await client.delete(
            f"/api-keys/{created['api_key']['id']}", headers=admin.headers
        )
        assert await verifier.verify_token(created["key"]) is None

    async def test_rejects_a_deactivated_users_token(
        self, verifier, client, admin, roleless
    ):
        await client.patch(
            f"/users/{roleless.user_id}",
            headers=admin.headers,
            json={"roles": []},
        )
        token = await verifier.verify_token(roleless.token)
        assert token.scopes == []


@pytest.fixture
def as_admin(monkeypatch, admin):
    """`retrieve_resume_data`/`list_resume_documents` read the caller's id and
    scopes off the MCP access token, which no direct call carries — the
    monkeypatch is the smaller stand-in for a real token context."""
    monkeypatch.setattr(ResumeTools, "current_user_id", lambda: admin.user_id)
    monkeypatch.setattr(ResumeTools, "current_scopes", lambda: frozenset(Scopes))
    return admin


@pytest.fixture
def as_narrowed(monkeypatch, admin):
    """The same caller, holding only the scopes a test names.

    This is what an MCP client actually looks like now: the owner's identity
    on a key cut down to what that client needs.
    """

    def narrow(*scopes: Scopes):
        monkeypatch.setattr(ResumeTools, "current_user_id", lambda: admin.user_id)
        monkeypatch.setattr(ResumeTools, "current_scopes", lambda: frozenset(scopes))
        return admin

    return narrow


def _text(result) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


async def retrieved(**kwargs) -> dict:
    """Unwraps the single text block `retrieve_resume_data` now returns, so
    the assertions below can keep subscripting a dict."""
    return json.loads(_text(await retrieve_resume_data(**kwargs)))


async def listed() -> dict:
    """Same unwrap as `retrieved`, for `list_resume_documents`."""
    return json.loads(_text(await list_resume_documents()))


class TestResumeTool:
    async def test_returns_resume_and_metadata(
        self, resume_payload, as_admin, stored_resume, stored_metadata
    ):
        result = await retrieved(
            resume_id="resume.json", resume_metadata_id="resume.metadata.json"
        )
        assert (
            result["resume"]["basics"]["summary"] == resume_payload["basics"]["summary"]
        )
        assert isinstance(result["resume_metadata"], dict)

    async def test_returns_the_skill_alongside_them(
        self, as_admin, stored_resume, stored_skill, skill_payload
    ):
        """One call has to carry all three: the skill is what the client
        follows, and it is worthless against a resume it did not arrive with."""
        result = await retrieved(
            resume_id="resume.json",
            resume_metadata_id="resume.metadata.json",
            resume_skill_id="resume.skill.json",
        )
        assert set(result) == {"resume", "resume_metadata", "resume_skill"}
        assert result["resume_skill"]["name"] == skill_payload["name"]
        assert result["resume_skill"]["procedure"][0]["id"] == "load"

    async def test_the_skill_is_the_latest_revision(
        self, client, as_admin, stored_resume, stored_skill, skill_payload
    ):
        """The point of centralizing it: editing the instructions is a document
        write, and the next run picks them up without a client release."""
        revised = {**skill_payload, "objective": "a differently worded objective"}
        await client.post(
            "/documents/skill",
            headers=as_admin.headers,
            json={"name": "resume.skill.json", "revision_note": "v2", "data": revised},
        )
        result = await retrieved(
            resume_id="resume.json", resume_skill_id="resume.skill.json"
        )
        assert result["resume_skill"]["objective"] == "a differently worded objective"

    async def test_companions_default_to_null_when_not_supplied(
        self, as_admin, stored_resume
    ):
        """Metadata previously returned the Document object, which is not
        serializable; an unsupplied companion is reported as null."""
        result = await retrieved(resume_id="resume.json")
        assert result["resume_metadata"] is None
        assert result["resume_skill"] is None

    async def test_a_missing_skill_id_does_not_fail_the_retrieval(
        self, as_admin, stored_resume
    ):
        """Only the resume is required — the client can say what it is working
        without, which beats returning nothing at all."""
        result = await retrieved(
            resume_id="resume.json", resume_skill_id="never-stored.json"
        )
        assert result["resume"] is not None
        assert result["resume_skill"] is None

    async def test_serves_private_fields(
        self, as_admin, stored_resume, private_markers
    ):
        """Runs against the slimmed payload `ResumeTools.slim` now produces —
        the change most likely to redact something by accident."""
        body = _text(await retrieve_resume_data(resume_id="resume.json"))
        for marker in private_markers:
            assert marker in body

    async def test_result_is_a_single_text_block(self, as_admin, stored_resume):
        """The whole point of Change A — otherwise invisible, since the tool
        still looks single-valued to a caller that only reads `.content`."""
        result = await retrieve_resume_data(resume_id="resume.json")
        assert result.structured_content is None
        assert len(result.content) == 1

    async def test_raises_when_no_resume_stored(self, as_admin):
        with pytest.raises(ToolError):
            await retrieve_resume_data(resume_id="resume.json")

    async def test_a_resume_id_naming_a_skill_document_raises(
        self, as_admin, stored_skill
    ):
        """Each id must resolve to a document of the matching type — a
        resume_id pointing at a skill is an error, not a silently wrong
        payload."""
        with pytest.raises(ToolError):
            await retrieve_resume_data(resume_id="resume.skill.json")

    async def test_a_metadata_id_naming_a_resume_document_raises(
        self, as_admin, stored_resume
    ):
        with pytest.raises(ToolError):
            await retrieve_resume_data(
                resume_id="resume.json", resume_metadata_id="resume.json"
            )

    async def test_cannot_read_another_users_document_by_guessing_its_name(
        self, monkeypatch, client, admin, other_owner, resume_payload
    ):
        await client.post(
            "/documents/resume",
            headers=other_owner.headers,
            json={"name": "resume.json", "revision_note": "v", "data": resume_payload},
        )
        monkeypatch.setattr(ResumeTools, "current_user_id", lambda: admin.user_id)

        with pytest.raises(ToolError):
            await retrieve_resume_data(resume_id="resume.json")


class TestListResumeDocumentsTool:
    async def test_lists_the_callers_documents(
        self, as_admin, stored_resume, stored_metadata, stored_skill
    ):
        result = await listed()
        ids = {doc["document_id"] for doc in result["documents"]}
        assert ids == {"resume.json", "resume.metadata.json", "resume.skill.json"}

    async def test_entries_carry_type_and_no_content(self, as_admin, stored_resume):
        result = await listed()
        entry = result["documents"][0]
        assert entry["type"] == "resume"
        assert "data" not in entry

    async def test_scoped_to_the_caller(
        self, as_admin, client, other_owner, resume_payload
    ):
        await client.post(
            "/documents/resume",
            headers=other_owner.headers,
            json={"name": "resume.json", "revision_note": "v", "data": resume_payload},
        )
        result = await listed()
        assert result["documents"] == []

    async def test_lists_only_the_types_the_credential_may_read(
        self, as_narrowed, stored_resume, stored_metadata, stored_skill
    ):
        """A key cut down to the skill document sees the skill document. It is
        the answer that credential should get, rather than a refusal for the
        two types it was deliberately not given."""
        as_narrowed(Scopes.SKILL_READ)
        result = await listed()
        assert {doc["document_id"] for doc in result["documents"]} == {
            "resume.skill.json"
        }

    async def test_result_is_a_single_text_block(self, as_admin, stored_resume):
        result = await list_resume_documents()
        assert result.structured_content is None
        assert len(result.content) == 1


class TestPerTypeScopesOverMcp:
    async def test_a_companion_id_needs_its_own_scope(
        self, as_narrowed, stored_resume, stored_metadata
    ):
        """Refused rather than answered with null: null means "not stored",
        and a client told that would work without a document that exists."""
        as_narrowed(Scopes.RESUME_READ)
        with pytest.raises(ToolError, match=Scopes.METADATA_READ.value):
            await retrieve_resume_data(
                resume_id="resume.json", resume_metadata_id="resume.metadata.json"
            )

    async def test_the_resume_alone_needs_no_companion_scope(
        self, as_narrowed, stored_resume
    ):
        as_narrowed(Scopes.RESUME_READ)
        result = await retrieved(resume_id="resume.json")
        assert result["resume"] is not None
        assert result["resume_metadata"] is None

    async def test_a_skill_id_needs_the_skill_scope(
        self, as_narrowed, stored_resume, stored_skill
    ):
        as_narrowed(Scopes.RESUME_READ, Scopes.METADATA_READ)
        with pytest.raises(ToolError, match=Scopes.SKILL_READ.value):
            await retrieve_resume_data(
                resume_id="resume.json", resume_skill_id="resume.skill.json"
            )


class TestMcpToolBaseOutsideAnyRequest:
    """`ResumeTools` and `TrackingTools` share these four helpers via
    `McpToolBase`; every other test monkeypatches `current_user_id` /
    `current_scopes`, so these cover what happens with no MCP access-token
    context at all - the state before any request has arrived."""

    def test_current_user_id_raises_without_a_token(self):
        with pytest.raises(ToolError):
            McpToolBase.current_user_id()

    def test_current_scopes_is_empty_without_a_token(self):
        assert McpToolBase.current_scopes() == frozenset()


class TestNoOutputSchema:
    """Pins the half of Change A that is easy to lose in a later refactor:
    the payload stays single-looking in a test that only checks
    `structured_content`, so the schema itself needs its own assertion."""

    def test_retrieve_resume_data_advertises_no_output_schema(self):
        assert FunctionTool.from_function(retrieve_resume_data).output_schema is None

    def test_list_resume_documents_advertises_no_output_schema(self):
        assert FunctionTool.from_function(list_resume_documents).output_schema is None


class TestSlimming:
    """Change B: `retrieve_resume_data` drops defaults on read via
    `ResumeTools.slim`."""

    async def _load(self, admin, name: str) -> Document:
        async with DatabaseService.session() as db:
            document = await DocumentReader(db, admin.user_id).latest(name)
        assert document is not None
        return document

    async def test_slimmed_resume_round_trips(self, admin, stored_resume):
        document = await self._load(admin, "resume.json")
        ResumePrivate.model_validate(ResumeTools.slim(document))

    async def test_slimmed_metadata_round_trips(self, admin, stored_metadata):
        document = await self._load(admin, "resume.metadata.json")
        ResumeMetadata.model_validate(ResumeTools.slim(document))

    async def test_slimmed_skill_round_trips(self, admin, stored_skill):
        document = await self._load(admin, "resume.skill.json")
        ResumeSkill.model_validate(ResumeTools.slim(document))

    async def test_fields_not_served_over_mcp_are_dropped(self, admin, stored_resume):
        """`keywords[].url` is stored, served by the REST routes, and
        deliberately withheld from an MCP read — see
        `ResumeTools.NOT_SERVED_OVER_MCP`."""
        document = await self._load(admin, "resume.json")
        slimmed = ResumeTools.slim(document)

        keyword = slimmed["skills"][0]["keywords"][0]
        assert "url" not in keyword
        assert keyword["name"] == "Python"

        # `basis` stays: it is required on `Metric`, so dropping it would
        # cost the round trip the tests above assert.
        assert slimmed["work"][0]["highlights"][0]["metrics"][0]["basis"]

    async def test_the_rest_route_still_serves_it(self, client, admin, stored_resume):
        """The MCP exclusion is about what a tailoring run is made to read,
        not about disclosure — no other surface may start withholding it. The
        website renders these links off the feed."""
        response = await client.get(
            "/documents/resume/resume.json", headers=admin.headers
        )
        assert response.status_code == 200, response.text
        assert "https://example.invalid/python" in json.dumps(response.json())

    async def test_metadata_documents_no_field_the_mcp_read_withholds(self):
        """A field documented but never delivered is the same class of bug as
        an alias mismatch: instructions pointing at something the client does
        not have. `NOT_SERVED_OVER_MCP` and the metadata document have to be
        changed together."""
        withheld = {"resume.skills[].keywords[].url"}
        metadata = json.loads(Path("data/resume.metadata.latest.json").read_text())
        documented = {
            field["path"]
            for section in metadata["sections"]
            for field in section.get("fields") or []
        }
        assert not (withheld & documented)

    async def test_publish_false_survives_slimming_but_true_does_not(
        self, client, admin, withheld_resume_payload
    ):
        """The withheld entry is the one case where the key carries
        information — the default (published) case drops it entirely."""
        response = await client.post(
            "/documents/resume",
            headers=admin.headers,
            json={
                "name": "resume.json",
                "revision_note": "v",
                "data": withheld_resume_payload,
            },
        )
        assert response.status_code == 200, response.text
        document = await self._load(admin, "resume.json")
        published_work, withheld_work = ResumeTools.slim(document)["work"]
        assert "publish" not in published_work
        assert withheld_work["publish"] is False

    async def test_the_resume_is_served_under_its_json_resume_field_names(
        self, admin, stored_resume
    ):
        """The MCP payload must spell fields the way the schema does, and the
        way the metadata and skill documents say it does.

        Multi-word fields are the only ones where the alias and the Python
        name differ, so they are the only ones that can regress. A dump that
        forgets `by_alias` hands the client `start_date` while every path in
        the two companion documents points at `startDate` — instructions
        aimed at fields the client never receives, which neither validation
        nor any other test in this file would notice.
        """
        document = await self._load(admin, "resume.json")
        slimmed = ResumeTools.slim(document)
        work = slimmed["work"][0]

        assert "startDate" in work
        assert "roleLocation" in work
        assert "lastUsed" in slimmed["skills"][0]["keywords"][0]
        assert self._snake_case_keys(slimmed) == []

    def _snake_case_keys(self, node, trail: str = "resume") -> list[str]:
        """Every key in the resume payload, at any depth, that is not spelled
        the way the schema spells it. Recursive rather than a handful of named
        fields so a section added later cannot quietly regress."""
        if isinstance(node, dict):
            return [f"{trail}.{key}" for key in node if "_" in key] + [
                name
                for key, value in node.items()
                for name in self._snake_case_keys(value, f"{trail}.{key}")
            ]
        if isinstance(node, list):
            return [
                name
                for index, item in enumerate(node)
                for name in self._snake_case_keys(item, f"{trail}[{index}]")
            ]
        return []

    async def test_a_document_that_no_longer_validates_still_retrieves(self, admin):
        """A document written under a since-tightened schema — here, missing
        every field `ResumePrivate` requires — must still come back as
        stored, not 500 on read."""
        raw = {"not": "a valid resume"}
        async with DatabaseService.session() as db:
            db.add(
                Document(
                    created_by=admin.user_id,
                    name="resume.json",
                    revision_id=1,
                    type=DocumentType.RESUME.value,
                    data=raw,
                    schema_version=1,
                )
            )
            await db.commit()
        document = await self._load(admin, "resume.json")
        assert ResumeTools.slim(document) == raw
