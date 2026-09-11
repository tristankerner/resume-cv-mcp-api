"""`ResumePatchService` and the `describe_resume_schema` /
`preview_resume_patch` MCP tool and the unified `confirm` — see
MCP_WRITEBACK_PLAN.md §4.1-4.4.
"""

import json
from typing import Any, get_args

import pytest
from fastmcp.exceptions import ToolError
from mcp.types import TextContent
from pydantic import TypeAdapter

from persistence.document import Document
from routers.mcp_confirm import confirm
from routers.mcp_documents import (
    describe_resume_schema,
    preview_resume_patch,
)
from services.auth.mcp_tools import McpToolBase
from services.auth.principal import CredentialKind, Principal
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService
from services.document.dtos.resume_object import (
    Engagement,
    LocationKind,
    Narrative,
    RoleLocation,
    SkillLevel,
)
from services.document.dtos.resume_patch import (
    AddHighlightOp,
    AddProjectOp,
    AddSkillKeywordOp,
    AddSpecificOp,
    AppendNarrativeOp,
    NarrativeField,
    PatchOp,
    SetLogisticsOp,
    SetSkillKeywordLevelOp,
)
from services.document.resume_patch_service import ResumePatchError, ResumePatchService

DOC_NAME = "resume.json"


def _text(result) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _principal(user_id: int, scopes: frozenset[Scopes] | None = None) -> Principal:
    return Principal(
        user_id=user_id,
        username="mcp",
        scopes=scopes if scopes is not None else frozenset(Scopes),
        credential=CredentialKind.API_KEY,
    )


async def _current_data(user_id: int, name: str = DOC_NAME) -> dict[str, Any]:
    async with DatabaseService.session() as db:
        doc = await Document.get_latest(db, user_id, name)
        assert doc is not None
        return doc.data


def _assert_only_additive(
    before: Any, after: Any, path: str = "$", ignore: frozenset[str] = frozenset()
) -> None:
    """Walks `before` against `after`: every key/index present in `before`
    must still be present in `after`, and every scalar must be unchanged
    unless `path` is in `ignore`. Extra keys/trailing list items in `after`
    are fine — that is what "additive" means.
    """
    if isinstance(before, dict):
        assert isinstance(after, dict), f"{path}: was a dict, is now {type(after)}"
        for key, value in before.items():
            assert key in after, f"{path}.{key}: present before, absent after"
            _assert_only_additive(value, after[key], f"{path}.{key}", ignore)
    elif isinstance(before, list):
        assert isinstance(after, list), f"{path}: was a list, is now {type(after)}"
        assert len(after) >= len(before), f"{path}: shrank from {before} to {after}"
        for index, value in enumerate(before):
            _assert_only_additive(value, after[index], f"{path}[{index}]", ignore)
    else:
        if path not in ignore:
            assert before == after, f"{path}: {before!r} -> {after!r}"


class TestDescribeResumeSchema:
    def test_lists_every_operation(self):
        description = ResumePatchService.describe()
        ops = {entry["op"] for entry in description["operations"]}
        assert ops == {
            "add_skill_keyword",
            "set_skill_keyword_level",
            "add_highlight",
            "add_specific",
            "add_project",
            "set_logistics",
            "append_narrative",
        }

    def test_vocabularies_match_the_live_literal_aliases(self):
        """Derived with get_args, not retyped — a hand-copied list here
        would silently drift from the model."""
        vocab = ResumePatchService.describe()["vocabularies"]
        assert set(vocab["skill_level"]) == set(get_args(SkillLevel))
        assert set(vocab["role_location"]) == set(get_args(RoleLocation))
        assert set(vocab["engagement"]) == set(get_args(Engagement))
        assert set(vocab["location_kind"]) == set(get_args(LocationKind))

    def test_narrative_field_literal_matches_the_model(self):
        assert set(get_args(NarrativeField)) == set(Narrative.model_fields)

    def test_is_built_once(self):
        assert ResumePatchService.describe() is ResumePatchService.describe()


class TestPreviewAndApply:
    async def test_add_skill_keyword(self, stored_resume, admin):
        ops = [
            {
                "op": "add_skill_keyword",
                "skill_group": "group",
                "name": "Rust",
                "level": "working",
            }
        ]
        parsed = TypeAdapter(list[PatchOp]).validate_python(ops)

        async with DatabaseService.session() as db:
            touched = await ResumePatchService(db, _principal(admin.user_id)).preview(
                DOC_NAME, parsed
            )
        assert touched[0].after["name"] == "Rust"

        async with DatabaseService.session() as db:
            result = await ResumePatchService(db, _principal(admin.user_id)).apply(
                DOC_NAME, parsed
            )
        assert result.revision_id == stored_resume["revision_id"] + 1

        after = await _current_data(admin.user_id)
        names = [k["name"] for k in after["skills"][0]["keywords"]]
        assert "Rust" in names

    async def test_add_skill_keyword_creates_a_missing_group(
        self, stored_resume, admin
    ):

        op = AddSkillKeywordOp(skill_group="New Group", name="Elixir")
        async with DatabaseService.session() as db:
            await ResumePatchService(db, _principal(admin.user_id)).apply(
                DOC_NAME, [op]
            )
        after = await _current_data(admin.user_id)
        groups = {g["name"]: g for g in after["skills"]}
        assert "New Group" in groups
        assert groups["New Group"]["keywords"][0]["name"] == "Elixir"

    async def test_add_skill_keyword_refuses_an_existing_name(
        self, stored_resume, admin
    ):

        op = AddSkillKeywordOp(skill_group="group", name="Python")
        async with DatabaseService.session() as db:
            with pytest.raises(ResumePatchError):
                await ResumePatchService(db, _principal(admin.user_id)).preview(
                    DOC_NAME, [op]
                )

    async def test_set_skill_keyword_level(self, stored_resume, admin):

        op = SetSkillKeywordLevelOp(
            skill_group="group", name="Python", level="familiar"
        )
        async with DatabaseService.session() as db:
            await ResumePatchService(db, _principal(admin.user_id)).apply(
                DOC_NAME, [op]
            )
        after = await _current_data(admin.user_id)
        assert after["skills"][0]["keywords"][0]["level"] == "familiar"

    async def test_set_skill_keyword_level_refuses_unknown_group(
        self, stored_resume, admin
    ):

        op = SetSkillKeywordLevelOp(skill_group="nope", name="Python", level="expert")
        async with DatabaseService.session() as db:
            with pytest.raises(ResumePatchError):
                await ResumePatchService(db, _principal(admin.user_id)).preview(
                    DOC_NAME, [op]
                )

    async def test_add_highlight(self, stored_resume, admin):

        op = AddHighlightOp(
            work_name="Company", id="highlight-2", summary="Did another thing"
        )
        async with DatabaseService.session() as db:
            await ResumePatchService(db, _principal(admin.user_id)).apply(
                DOC_NAME, [op]
            )
        after = await _current_data(admin.user_id)
        ids = [h["id"] for h in after["work"][0]["highlights"]]
        assert ids == ["highlight-1", "highlight-2"]

    async def test_add_highlight_refuses_unknown_work(self, stored_resume, admin):

        op = AddHighlightOp(work_name="Nope Corp", id="x", summary="y")
        async with DatabaseService.session() as db:
            with pytest.raises(ResumePatchError):
                await ResumePatchService(db, _principal(admin.user_id)).preview(
                    DOC_NAME, [op]
                )

    async def test_add_highlight_refuses_a_duplicate_id(self, stored_resume, admin):

        op = AddHighlightOp(work_name="Company", id="highlight-1", summary="y")
        async with DatabaseService.session() as db:
            with pytest.raises(ResumePatchError, match="highlight-1"):
                await ResumePatchService(db, _principal(admin.user_id)).preview(
                    DOC_NAME, [op]
                )

    async def test_add_specific(self, stored_resume, admin):

        op = AddSpecificOp(highlight_id="highlight-1", detail="new evidence")
        async with DatabaseService.session() as db:
            await ResumePatchService(db, _principal(admin.user_id)).apply(
                DOC_NAME, [op]
            )
        after = await _current_data(admin.user_id)
        details = [s["detail"] for s in after["work"][0]["highlights"][0]["specifics"]]
        assert "new evidence" in details
        # The summary the op is not allowed to touch stays exactly as it was.
        assert after["work"][0]["highlights"][0]["summary"] == "did a thing"

    async def test_add_specific_refuses_unknown_highlight(self, stored_resume, admin):

        op = AddSpecificOp(highlight_id="nope", detail="x")
        async with DatabaseService.session() as db:
            with pytest.raises(ResumePatchError):
                await ResumePatchService(db, _principal(admin.user_id)).preview(
                    DOC_NAME, [op]
                )

    async def test_add_project(self, stored_resume, admin):

        op = AddProjectOp(name="New Project", description="a new one")
        async with DatabaseService.session() as db:
            await ResumePatchService(db, _principal(admin.user_id)).apply(
                DOC_NAME, [op]
            )
        after = await _current_data(admin.user_id)
        names = [p["name"] for p in after["projects"]]
        assert "New Project" in names

    async def test_add_project_with_highlights_refuses_a_collision(
        self, stored_resume, admin
    ):

        op = AddProjectOp(
            name="New Project",
            highlights=[{"id": "highlight-1", "summary": "collides"}],
        )
        async with DatabaseService.session() as db:
            with pytest.raises(ResumePatchError, match="highlight-1"):
                await ResumePatchService(db, _principal(admin.user_id)).preview(
                    DOC_NAME, [op]
                )

    async def test_set_logistics_only_touches_given_fields(self, stored_resume, admin):

        op = SetLogisticsOp(travel="Up to 25%")
        async with DatabaseService.session() as db:
            await ResumePatchService(db, _principal(admin.user_id)).apply(
                DOC_NAME, [op]
            )
        after = await _current_data(admin.user_id)
        logistics = after["fineTuningData"]["logistics"]
        assert logistics["travel"] == "Up to 25%"
        # salaryExpectation was already set on the fixture and set_logistics
        # did not name it, so it must be untouched.
        assert logistics["salaryExpectation"].startswith("salary-")

    async def test_append_narrative_appends_rather_than_replaces(
        self, stored_resume, admin
    ):

        original = (await _current_data(admin.user_id))["fineTuningData"]["narrative"][
            "voice"
        ]
        op = AppendNarrativeOp(field="voice", text="Also direct in writing.")
        async with DatabaseService.session() as db:
            await ResumePatchService(db, _principal(admin.user_id)).apply(
                DOC_NAME, [op]
            )
        after = await _current_data(admin.user_id)
        new_voice = after["fineTuningData"]["narrative"]["voice"]
        assert new_voice.startswith(original)
        assert "Also direct in writing." in new_voice

    async def test_append_narrative_on_an_absent_field_just_sets_it(
        self, stored_resume, admin
    ):

        op = AppendNarrativeOp(field="career_arc", text="Started in backend.")
        async with DatabaseService.session() as db:
            await ResumePatchService(db, _principal(admin.user_id)).apply(
                DOC_NAME, [op]
            )
        after = await _current_data(admin.user_id)
        assert (
            after["fineTuningData"]["narrative"]["careerArc"] == "Started in backend."
        )

    async def test_unknown_document_is_refused(self, admin):

        op = AppendNarrativeOp(field="voice", text="x")
        async with DatabaseService.session() as db:
            with pytest.raises(ResumePatchError):
                await ResumePatchService(db, _principal(admin.user_id)).preview(
                    "nonexistent.json", [op]
                )

    async def test_a_preview_never_committed_writes_nothing(self, stored_resume, admin):

        op = AppendNarrativeOp(field="voice", text="never committed")
        async with DatabaseService.session() as db:
            await ResumePatchService(db, _principal(admin.user_id)).preview(
                DOC_NAME, [op]
            )
        after = await _current_data(admin.user_id)
        assert "never committed" not in after["fineTuningData"]["narrative"]["voice"]


class TestNonDestructiveInvariant:
    """Every op, applied to the fixture document, leaves every path that
    existed before still present, and changes no scalar it does not name —
    see MCP_WRITEBACK_PLAN.md §4.3."""

    async def _apply_and_diff(
        self, admin, op, ignore: frozenset[str] = frozenset()
    ) -> None:
        before = await _current_data(admin.user_id)
        async with DatabaseService.session() as db:
            await ResumePatchService(db, _principal(admin.user_id)).apply(
                DOC_NAME, [op]
            )
        after = await _current_data(admin.user_id)
        _assert_only_additive(before, after, ignore=ignore)

    async def test_add_skill_keyword(self, stored_resume, admin):

        await self._apply_and_diff(
            admin, AddSkillKeywordOp(skill_group="group", name="Rust")
        )

    async def test_add_highlight(self, stored_resume, admin):

        await self._apply_and_diff(
            admin,
            AddHighlightOp(work_name="Company", id="highlight-2", summary="new"),
        )

    async def test_add_specific(self, stored_resume, admin):

        await self._apply_and_diff(
            admin, AddSpecificOp(highlight_id="highlight-1", detail="more evidence")
        )

    async def test_add_project(self, stored_resume, admin):

        await self._apply_and_diff(admin, AddProjectOp(name="New Project"))

    async def test_set_logistics(self, stored_resume, admin):
        """The named field goes from unset to a value — filling in an
        absent field is not "rewriting existing content", so it is allowed
        the same way `set_skill_keyword_level`'s named field is."""
        await self._apply_and_diff(
            admin,
            SetLogisticsOp(travel="Up to 10%"),
            ignore=frozenset({"$.fineTuningData.logistics.travel"}),
        )

    async def test_set_skill_keyword_level(self, stored_resume, admin):
        """The one deliberate exception: the named field is allowed to
        change, and nothing else may."""

        await self._apply_and_diff(
            admin,
            SetSkillKeywordLevelOp(
                skill_group="group", name="Python", level="familiar"
            ),
            ignore=frozenset({"$.skills[0].keywords[0].level"}),
        )

    async def test_append_narrative(self, stored_resume, admin):
        """The named narrative field is allowed to change (it grows, it is
        not replaced-with-something-unrelated); nothing else may."""

        await self._apply_and_diff(
            admin,
            AppendNarrativeOp(field="voice", text="More."),
            ignore=frozenset({"$.fineTuningData.narrative.voice"}),
        )


class TestMcpToolLayer:
    """The registered `@tool` adapters and the `DocumentPatchTools`
    classmethods they delegate to — same shape as `test_mcp_tracking.py`."""

    @pytest.fixture
    def as_admin(self, monkeypatch, admin):
        # Patched on the base rather than on `DocumentPatchTools`: `confirm`
        # reads the credential through `ConfirmTools`, a sibling subclass that
        # would not see an attribute shadowed on this one.
        monkeypatch.setattr(McpToolBase, "current_user_id", lambda: admin.user_id)
        monkeypatch.setattr(McpToolBase, "current_scopes", lambda: frozenset(Scopes))
        return admin

    async def test_describe_resume_schema_returns_operations(self, as_admin):
        result = await describe_resume_schema()

        body = json.loads(_text(result))
        assert len(body["operations"]) == 7

    async def test_preview_then_confirm(self, as_admin, stored_resume):
        preview_result = await preview_resume_patch(
            DOC_NAME,
            [{"op": "append_narrative", "field": "strengths", "text": "Debugging."}],
        )

        preview_body = json.loads(_text(preview_result))
        assert preview_body["preview"]["touched"][0]["after"] == "Debugging."

        confirm_result = await confirm(preview_body["confirm_token"])
        confirm_body = json.loads(_text(confirm_result))["result"]
        assert confirm_body["revision_id"] == stored_resume["revision_id"] + 1

    async def test_confirming_twice_is_refused(self, as_admin, stored_resume):

        preview_result = await preview_resume_patch(
            DOC_NAME,
            [{"op": "append_narrative", "field": "strengths", "text": "x"}],
        )
        preview_body = json.loads(_text(preview_result))
        await confirm(preview_body["confirm_token"])
        with pytest.raises(ToolError):
            await confirm(preview_body["confirm_token"])

    async def test_invalid_op_is_refused_before_any_token_is_issued(
        self, as_admin, stored_resume
    ):
        with pytest.raises(ToolError):
            await preview_resume_patch(
                DOC_NAME,
                [
                    {
                        "op": "add_highlight",
                        "work_name": "Nope Corp",
                        "id": "x",
                        "summary": "y",
                    }
                ],
            )
