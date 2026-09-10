"""The closed set of resume write-back operations MCP may perform — see
`describe_resume_schema` and MCP_WRITEBACK_PLAN.md §4.3.

Each op is a typed, `extra="forbid"` Pydantic model. There is no operation
that accepts an arbitrary dotted path or an arbitrary `dict[str, Any]` value
— an unsupported property has nowhere to go, rejected by the tool's own
parameter schema before any document is loaded. Resist adding either; both
would reintroduce exactly the failure this design exists to prevent. Adding a
new op is a code change and a test, deliberately — every op here is
non-destructive by construction (see `services.document.resume_patch_service
.ResumePatchService`'s module docstring), and `tests/test_resume_patch.py`
proves that invariant for each one.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from services.document.dtos.resume_object import Highlight, Metric, SkillLevel, Specific

# Hand-written rather than derived from `Narrative.model_fields` at import
# time, for the same reason `ApplicationStatusLiteral` is in
# routers/mcp_tracking.py: `Literal` only accepts literal constants,
# not a computed expression. `tests/test_resume_patch.py` asserts this stays
# equal to `Narrative`'s field names, which is what catches drift instead.
NarrativeField = Literal[
    "career_arc",
    "current_status",
    "motivation",
    "looking_for",
    "avoiding",
    "working_style",
    "strengths",
    "voice",
]


class PatchOpBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AddSkillKeywordOp(PatchOpBase):
    """Add a new skill keyword to a named group, creating the group if it
    does not exist yet."""

    op: Literal["add_skill_keyword"] = "add_skill_keyword"
    skill_group: str
    name: str
    level: SkillLevel | None = None
    last_used: str | None = None
    url: str | None = None


class SetSkillKeywordLevelOp(PatchOpBase):
    """The one deliberate exception to "no edits" — see the module this op
    is applied from for why. Scoped to `level` and `last_used` only."""

    op: Literal["set_skill_keyword_level"] = "set_skill_keyword_level"
    skill_group: str
    name: str
    level: SkillLevel
    last_used: str | None = None


class AddHighlightOp(PatchOpBase):
    """Add a new highlight to an existing `work[]` entry. `id` must be
    unique across every `work[]`, `volunteer[]` and `projects[]` highlight in
    the document."""

    op: Literal["add_highlight"] = "add_highlight"
    work_name: str
    id: str
    summary: str
    specifics: list[Specific] = []
    tech: list[str] = []
    metrics: list[Metric] = []
    story: str | None = None


class AddSpecificOp(PatchOpBase):
    """Add evidence under an existing highlight, without touching its
    `summary`."""

    op: Literal["add_specific"] = "add_specific"
    highlight_id: str
    detail: str
    tech: list[str] = []


class AddProjectOp(PatchOpBase):
    """Add a new project. `highlights` takes the v3 object shape — every id
    must be unique the same way `add_highlight`'s is."""

    op: Literal["add_project"] = "add_project"
    name: str
    description: str | None = None
    url: str | None = None
    highlights: list[Highlight] = []
    keywords: list[str] = []
    start_date: str | None = None
    end_date: str | None = None
    roles: list[str] = []
    entity: str | None = None
    type: str | None = None


class SetLogisticsOp(PatchOpBase):
    """Each field is independently optional; a field left unset is
    unchanged, and there is no way to express "clear this"."""

    op: Literal["set_logistics"] = "set_logistics"
    work_authorization: str | None = None
    salary_expectation: str | None = None
    availability: str | None = None
    relocation: str | None = None
    on_site: str | None = None
    travel: str | None = None


class AppendNarrativeOp(PatchOpBase):
    """Appends a sentence to one `Narrative` field; never replaces it."""

    op: Literal["append_narrative"] = "append_narrative"
    field: NarrativeField
    text: str


PatchOp = Annotated[
    AddSkillKeywordOp
    | SetSkillKeywordLevelOp
    | AddHighlightOp
    | AddSpecificOp
    | AddProjectOp
    | SetLogisticsOp
    | AppendNarrativeOp,
    Field(discriminator="op"),
]
