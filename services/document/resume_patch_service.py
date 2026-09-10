"""Resume write-back through a closed set of patch operations — see
MCP_WRITEBACK_PLAN.md §4.3-4.4. The model never hands over a whole document;
each op is a typed operation resolved against the latest revision and
applied to an in-memory copy before anything is written.

Non-destructive by construction: no op removes a `work[]` entry, deletes a
highlight, rewrites an existing `summary`, renames a document, or flips
`publish`. `tests/test_resume_patch.py` asserts this holds for every op —
applying it to a fixture document never makes a path that existed before
absent afterward, and never changes a scalar the op does not name.
"""

from typing import Annotated, Any, ClassVar, NamedTuple, get_args

from fastapi import Depends
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.document import Document
from services.auth.auth_service import AuthService
from services.auth.exceptions import AuthErrors
from services.auth.principal import Principal
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService
from services.document.document_reader import DocumentReader
from services.document.document_types import DocumentType, DocumentTypeRegistry
from services.document.dtos.create_document import CreateDocumentResponse, UpsertStatus
from services.document.dtos.resume_object import (
    Engagement,
    FineTuningData,
    Highlight,
    Keyword,
    LocationKind,
    Logistics,
    Narrative,
    Project,
    ResumePrivate,
    RoleLocation,
    Skill,
    SkillLevel,
    Specific,
)
from services.document.dtos.resume_patch import (
    AddHighlightOp,
    AddProjectOp,
    AddSkillKeywordOp,
    AddSpecificOp,
    AppendNarrativeOp,
    PatchOp,
    SetLogisticsOp,
    SetSkillKeywordLevelOp,
)
from services.service_interface import ServiceProviderInterface


class TouchedPath(NamedTuple):
    """One field the patch changed, for the preview's before/after."""

    path: str
    before: Any
    after: Any


class ResumePatchError(Exception):
    """One op could not be applied. The message is written to be shown to
    the user verbatim."""


class ResumePatchService(ServiceProviderInterface):
    """`describe()`, `preview(ops)`, and `apply(ops)` — see the module
    docstring. Read scoped through `DocumentReader` and written through
    `Document.upsert_document`, the same primitives `DocumentService` and
    `ResumeTools` use, so ownership scoping and revision semantics cannot
    drift between the HTTP, read-MCP and write-MCP surfaces.
    """

    _DESCRIPTION: ClassVar[dict[str, Any] | None] = None

    def __init__(self, db: AsyncSession, principal: Principal | None):
        self.db = db
        self.principal = principal

    def _require(self, scope: Scopes) -> None:
        if self.principal is None:
            raise AuthErrors.credentials()
        self.principal.require_scope(scope)

    def _owner(self) -> int:
        if self.principal is None:
            raise AuthErrors.credentials()
        return self.principal.user_id

    def _reader(self) -> DocumentReader:
        return DocumentReader(self.db, self._owner())

    # --- describe -----------------------------------------------------

    @classmethod
    def describe(cls) -> dict[str, Any]:
        """Built once, at class level where it can be — see §4.2 of the
        plan. Vocabularies are read from the `Literal` aliases in
        `resume_object.py` with `get_args`, not retyped: a hand-copied list
        here would silently drift from the model, the same class of bug
        `ApplicationStatusLiteral` in `mcp_tracking.py` guards against with a
        test — `tests/test_resume_patch.py` asserts the equivalent here.
        """
        if cls._DESCRIPTION is None:
            cls._DESCRIPTION = cls._build_description()
        return cls._DESCRIPTION

    @staticmethod
    def _build_description() -> dict[str, Any]:
        return {
            "not_changeable": (
                "No operation here can rename or delete anything, edit an "
                "existing highlight's summary, flip `publish`, or write "
                "metadata or skill documents. There is no operation that "
                "takes an arbitrary path or an arbitrary value — every "
                "field an operation can touch is named explicitly below."
            ),
            "iso8601_rule": (
                "Any date-shaped field accepts YYYY, YYYY-MM, or "
                "YYYY-MM-DD — nothing else. This is a rule on the model, "
                "not something visible as a regex to you: a value in any "
                "other shape is refused."
            ),
            "vocabularies": {
                "skill_level": sorted(get_args(SkillLevel)),
                "role_location": sorted(get_args(RoleLocation)),
                "engagement": sorted(get_args(Engagement)),
                "location_kind": sorted(get_args(LocationKind)),
            },
            "operations": [
                {
                    "op": "add_skill_keyword",
                    "arguments": {
                        "skill_group": "str — the group name; created if it "
                        "does not exist yet, as part of the same "
                        "confirmation",
                        "name": "str",
                        "level": "one of vocabularies.skill_level, optional",
                        "last_used": "Iso8601, optional",
                        "url": "str, optional",
                    },
                    "example": {
                        "op": "add_skill_keyword",
                        "skill_group": "Languages & frameworks",
                        "name": "Rust",
                        "level": "working",
                        "last_used": "2026",
                    },
                },
                {
                    "op": "set_skill_keyword_level",
                    "arguments": {
                        "skill_group": "str — must already exist",
                        "name": "str — must already exist in that group",
                        "level": "one of vocabularies.skill_level",
                        "last_used": "Iso8601, optional",
                    },
                    "example": {
                        "op": "set_skill_keyword_level",
                        "skill_group": "Languages & frameworks",
                        "name": "Go",
                        "level": "expert",
                    },
                },
                {
                    "op": "add_highlight",
                    "arguments": {
                        "work_name": "str — must match an existing work[].name",
                        "id": "str — unique across every work[], "
                        "volunteer[] and projects[] highlight",
                        "summary": "str",
                        "specifics": "list of {detail, tech?}, optional",
                        "tech": "list[str], optional",
                        "metrics": "list of {figure, amount, basis}, optional",
                        "story": "str, optional",
                    },
                    "example": {
                        "op": "add_highlight",
                        "work_name": "Fictional Payments Co.",
                        "id": "fpc-new-highlight",
                        "summary": "Led the migration to a new queue.",
                        "tech": ["Python"],
                    },
                },
                {
                    "op": "add_specific",
                    "arguments": {
                        "highlight_id": "str — must match an existing "
                        "highlight anywhere in the document",
                        "detail": "str",
                        "tech": "list[str], optional",
                    },
                    "example": {
                        "op": "add_specific",
                        "highlight_id": "fpc-idempotency-rework",
                        "detail": "Added a monitoring dashboard for retry volume.",
                    },
                },
                {
                    "op": "add_project",
                    "arguments": {
                        "name": "str",
                        "description": "str, optional",
                        "url": "str, optional",
                        "highlights": "list of {id, summary, ...}, the "
                        "same shape add_highlight builds, optional",
                        "keywords": "list[str], optional",
                        "start_date": "Iso8601, optional",
                        "end_date": "Iso8601, optional",
                        "roles": "list[str], optional",
                        "entity": "str, optional",
                        "type": "str, optional",
                    },
                    "example": {
                        "op": "add_project",
                        "name": "example-status-page",
                        "description": "A minimal static status-page generator.",
                    },
                },
                {
                    "op": "set_logistics",
                    "arguments": {
                        "work_authorization": "str, optional",
                        "salary_expectation": "str, optional",
                        "availability": "str, optional",
                        "relocation": "str, optional",
                        "on_site": "str, optional",
                        "travel": "str, optional — everything that is "
                        "not the ordinary commute; distinct from on_site",
                    },
                    "example": {
                        "op": "set_logistics",
                        "travel": "Up to 25% for on-sites and offsites.",
                    },
                },
                {
                    "op": "append_narrative",
                    "arguments": {
                        "field": "one of career_arc, current_status, "
                        "motivation, looking_for, avoiding, "
                        "working_style, strengths, voice",
                        "text": "str — appended, never replaces the existing value",
                    },
                    "example": {
                        "op": "append_narrative",
                        "field": "looking_for",
                        "text": "Open to platform roles, not just backend.",
                    },
                },
            ],
        }

    # --- load -----------------------------------------------------------

    async def _load(self, document_name: str) -> ResumePrivate:
        document = await self._reader().latest(document_name)
        if document is None or document.type != DocumentType.RESUME:
            raise ResumePatchError(f"No resume document named {document_name!r}.")
        try:
            return ResumePrivate.model_validate(document.data)
        except ValidationError as error:
            raise ResumePatchError(
                f"{document_name!r}'s stored document does not validate "
                "against the current resume schema, so a patch cannot be "
                f"appended to it: {error}"
            ) from error

    # --- preview / apply --------------------------------------------------

    async def preview(
        self, document_name: str, ops: list[PatchOp]
    ) -> list[TouchedPath]:
        """Loads, applies to a copy, and re-validates — never writes.
        Requires both `resume:read` and `resume:write`: it reads the
        existing document to resolve every op, and it is the read half of a
        write the caller has not yet committed to.
        """
        self._require(Scopes.RESUME_READ)
        self._require(Scopes.RESUME_WRITE)
        resume = await self._load(document_name)
        touched = self._apply_ops(resume, ops)
        self._revalidate(resume)
        return touched

    async def apply(
        self, document_name: str, ops: list[PatchOp]
    ) -> CreateDocumentResponse:
        """The same application `preview` performs, then appended as a new
        revision through `Document.upsert_document` — the same primitive
        `DocumentService._upsert` uses, so the revision note and schema
        stamping are identical to the HTTP path.
        """
        self._require(Scopes.RESUME_WRITE)
        resume = await self._load(document_name)
        self._apply_ops(resume, ops)
        self._revalidate(resume)

        document = Document(
            created_by=self._owner(),
            name=document_name,
            type=DocumentType.RESUME.value,
            revision_note=self._revision_note(ops),
            data=resume.model_dump(by_alias=True),
            schema_version=DocumentTypeRegistry.CURRENT_SCHEMA_VERSION_BY_TYPE[
                DocumentType.RESUME
            ],
        )
        result = await Document.upsert_document(self.db, document)
        if result.document is None:
            raise ResumePatchError("The patch produced no document to write.")
        return CreateDocumentResponse(
            name=result.document.name,
            revision_id=result.document.revision_id,
            status=UpsertStatus.CREATED if result.created else UpsertStatus.UNCHANGED,
        )

    @staticmethod
    def _revalidate(resume: ResumePrivate) -> None:
        """A patched document that no longer validates is refused, the same
        way `create_document` refuses one on the way in — never written."""
        try:
            ResumePrivate.model_validate(resume.model_dump(by_alias=True))
        except ValidationError as error:
            raise ResumePatchError(
                f"This patch would leave the document invalid: {error}"
            ) from error

    @staticmethod
    def _revision_note(ops: list[PatchOp]) -> str:
        """Generated, not model-supplied — a model-written note is a place
        for it to describe something other than what it did."""
        counts: dict[str, int] = {}
        for op in ops:
            counts[op.op] = counts.get(op.op, 0) + 1
        parts = [f"{count} {name}" for name, count in counts.items()]
        return "MCP: " + ", ".join(parts)

    # --- op application ---------------------------------------------------

    def _apply_ops(
        self, resume: ResumePrivate, ops: list[PatchOp]
    ) -> list[TouchedPath]:
        touched: list[TouchedPath] = []
        for op in ops:
            if isinstance(op, AddSkillKeywordOp):
                touched.append(self._add_skill_keyword(resume, op))
            elif isinstance(op, SetSkillKeywordLevelOp):
                touched.append(self._set_skill_keyword_level(resume, op))
            elif isinstance(op, AddHighlightOp):
                touched.append(self._add_highlight(resume, op))
            elif isinstance(op, AddSpecificOp):
                touched.append(self._add_specific(resume, op))
            elif isinstance(op, AddProjectOp):
                touched.append(self._add_project(resume, op))
            elif isinstance(op, SetLogisticsOp):
                touched.extend(self._set_logistics(resume, op))
            elif isinstance(op, AppendNarrativeOp):
                touched.append(self._append_narrative(resume, op))
            else:
                raise ResumePatchError(f"Unknown operation: {op!r}")
        return touched

    @staticmethod
    def _all_highlight_ids(resume: ResumePrivate) -> dict[str, str]:
        """Every highlight id in the document, mapped to a label for the
        entry it belongs to — for the collision message `add_highlight` and
        `add_project` need."""
        ids: dict[str, str] = {}
        for work in resume.work:
            for highlight in work.highlights:
                ids[highlight.id] = f"work[{work.name!r}]"
        for volunteer in resume.volunteer:
            for highlight in volunteer.highlights:
                label = volunteer.organization or "volunteer"
                ids[highlight.id] = f"volunteer[{label!r}]"
        for project in resume.projects:
            for highlight in project.highlights:
                label = project.name or "project"
                ids[highlight.id] = f"projects[{label!r}]"
        return ids

    def _add_skill_keyword(
        self, resume: ResumePrivate, op: AddSkillKeywordOp
    ) -> TouchedPath:
        group = next((s for s in resume.skills if s.name == op.skill_group), None)
        if group is None:
            group = Skill(name=op.skill_group, keywords=[])
            resume.skills.append(group)
        if any(keyword.name.lower() == op.name.lower() for keyword in group.keywords):
            raise ResumePatchError(
                f"{op.name!r} already exists in skill group {op.skill_group!r}. "
                "Use set_skill_keyword_level to change its level instead."
            )
        keyword = Keyword(
            name=op.name, level=op.level, last_used=op.last_used, url=op.url
        )
        group.keywords.append(keyword)
        return TouchedPath(
            path=f"skills[{op.skill_group!r}].keywords[]",
            before=None,
            after=keyword.model_dump(by_alias=True, exclude_none=True),
        )

    def _set_skill_keyword_level(
        self, resume: ResumePrivate, op: SetSkillKeywordLevelOp
    ) -> TouchedPath:
        group = next((s for s in resume.skills if s.name == op.skill_group), None)
        if group is None:
            raise ResumePatchError(f"No skill group named {op.skill_group!r}.")
        keyword = next(
            (k for k in group.keywords if k.name.lower() == op.name.lower()), None
        )
        if keyword is None:
            raise ResumePatchError(
                f"No skill keyword named {op.name!r} in group {op.skill_group!r}."
            )
        before = {"level": keyword.level, "last_used": keyword.last_used}
        keyword.level = op.level
        if op.last_used is not None:
            keyword.last_used = op.last_used
        after = {"level": keyword.level, "last_used": keyword.last_used}
        return TouchedPath(
            path=f"skills[{op.skill_group!r}].keywords[{op.name!r}]",
            before=before,
            after=after,
        )

    def _add_highlight(self, resume: ResumePrivate, op: AddHighlightOp) -> TouchedPath:
        work = next((w for w in resume.work if w.name == op.work_name), None)
        if work is None:
            raise ResumePatchError(f"No work[] entry named {op.work_name!r}.")
        existing_ids = self._all_highlight_ids(resume)
        if op.id in existing_ids:
            raise ResumePatchError(
                f"Highlight id {op.id!r} already exists, in "
                f"{existing_ids[op.id]}. Highlight ids must be unique across "
                "the whole document."
            )
        highlight = Highlight(
            id=op.id,
            summary=op.summary,
            specifics=list(op.specifics),
            tech=list(op.tech),
            metrics=list(op.metrics),
            story=op.story,
        )
        work.highlights.append(highlight)
        return TouchedPath(
            path=f"work[{op.work_name!r}].highlights[]",
            before=None,
            after=highlight.model_dump(by_alias=True, exclude_none=True),
        )

    def _add_specific(self, resume: ResumePrivate, op: AddSpecificOp) -> TouchedPath:
        for entries in (resume.work, resume.volunteer, resume.projects):
            for entry in entries:
                for highlight in entry.highlights:
                    if highlight.id == op.highlight_id:
                        before = [s.model_dump() for s in highlight.specifics]
                        highlight.specifics.append(
                            Specific(detail=op.detail, tech=list(op.tech))
                        )
                        after = [s.model_dump() for s in highlight.specifics]
                        return TouchedPath(
                            path=f"highlights[{op.highlight_id!r}].specifics[]",
                            before=before,
                            after=after,
                        )
        raise ResumePatchError(f"No highlight with id {op.highlight_id!r}.")

    def _add_project(self, resume: ResumePrivate, op: AddProjectOp) -> TouchedPath:
        existing_ids = self._all_highlight_ids(resume)
        for highlight in op.highlights:
            if highlight.id in existing_ids:
                raise ResumePatchError(
                    f"Highlight id {highlight.id!r} already exists, in "
                    f"{existing_ids[highlight.id]}. Highlight ids must be "
                    "unique across the whole document."
                )
        seen: set[str] = set()
        for highlight in op.highlights:
            if highlight.id in seen:
                raise ResumePatchError(
                    f"Highlight id {highlight.id!r} is repeated within this "
                    "project's own highlights."
                )
            seen.add(highlight.id)

        project = Project(
            name=op.name,
            description=op.description,
            url=op.url,
            highlights=list(op.highlights),
            keywords=list(op.keywords),
            start_date=op.start_date,
            end_date=op.end_date,
            roles=list(op.roles),
            entity=op.entity,
            type=op.type,
        )
        resume.projects.append(project)
        return TouchedPath(
            path="projects[]",
            before=None,
            after=project.model_dump(by_alias=True, exclude_none=True),
        )

    def _set_logistics(
        self, resume: ResumePrivate, op: SetLogisticsOp
    ) -> list[TouchedPath]:
        if resume.fine_tuning_data is None:
            resume.fine_tuning_data = FineTuningData()
        if resume.fine_tuning_data.logistics is None:
            resume.fine_tuning_data.logistics = Logistics()
        logistics = resume.fine_tuning_data.logistics

        touched: list[TouchedPath] = []
        fields_set = op.model_fields_set - {"op"}
        for field in fields_set:
            before = getattr(logistics, field)
            after = getattr(op, field)
            setattr(logistics, field, after)
            touched.append(
                TouchedPath(
                    path=f"fineTuningData.logistics.{field}",
                    before=before,
                    after=after,
                )
            )
        return touched

    def _append_narrative(
        self, resume: ResumePrivate, op: AppendNarrativeOp
    ) -> TouchedPath:
        if resume.fine_tuning_data is None:
            resume.fine_tuning_data = FineTuningData()
        if resume.fine_tuning_data.narrative is None:
            resume.fine_tuning_data.narrative = Narrative()
        narrative = resume.fine_tuning_data.narrative

        before = getattr(narrative, op.field)
        after = f"{before.rstrip()} {op.text}".strip() if before else op.text
        setattr(narrative, op.field, after)
        return TouchedPath(
            path=f"fineTuningData.narrative.{op.field}", before=before, after=after
        )

    @staticmethod
    def get_with_deps(
        db: Annotated[AsyncSession, Depends(DatabaseService.get_async_db_session)],
        principal: Annotated[
            Principal | None, Depends(AuthService.get_optional_principal)
        ],
    ) -> ResumePatchService:
        return ResumePatchService(db, principal)
