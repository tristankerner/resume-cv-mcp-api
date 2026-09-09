from datetime import date
from typing import Annotated, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.common.datetimes import UtcDatetime
from services.tracking.dtos.application_event import ApplicationEvent
from services.tracking.dtos.attachment import AttachmentMeta
from services.tracking.dtos.common import DocumentRef, HttpUrlText
from services.tracking.enums import ApplicationStatus

JobCode = Annotated[str | None, Field(default=None, max_length=100)]


class DocumentRefRule:
    """The one rule both application request models share.

    A class rather than a module-level function and constant: `CLAUDE.md`
    allows neither outside the documented exceptions, and this file is not one
    of them.
    """

    PREFIXES: ClassVar[tuple[str, ...]] = ("resume", "metadata", "skill")

    @classmethod
    def check(cls, model: BaseModel) -> None:
        """Each `<type>_document_name` / `<type>_revision_id` pair must be
        given together or not at all - a half-stated reference is
        meaningless."""
        for prefix in cls.PREFIXES:
            name = getattr(model, f"{prefix}_document_name")
            revision_id = getattr(model, f"{prefix}_revision_id")
            if (name is None) != (revision_id is None):
                raise ValueError(
                    f"{prefix}_document_name and {prefix}_revision_id must be "
                    "given together or not at all."
                )


class ApplicationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    company_id: int
    company_name: str
    job_title: str | None
    job_code: str | None
    # How many *other* applications share this job code. The point of the
    # field: two recruiters submitting the same requisition show up as one
    # number in a list rather than as two rows nobody connects.
    job_code_match_count: int
    url: str | None
    source: str | None
    system: str | None
    status: ApplicationStatus
    status_label: str
    status_changed_at: UtcDatetime | None
    date_submitted: date | None
    manually_modified: bool
    resume_label: str | None
    event_count: int
    attachment_count: int
    created_at: UtcDatetime
    updated_at: UtcDatetime


class ApplicationDetail(ApplicationSummary):
    job_description: str | None
    initial_prompt_text: str | None
    modification_note: str | None
    resume_document: DocumentRef | None
    metadata_document: DocumentRef | None
    skill_document: DocumentRef | None
    events: list[ApplicationEvent]
    attachments: list[AttachmentMeta]
    # The other applications carrying the same job code, whichever company or
    # recruiter they came through. Empty when the code is unset or unique.
    related_by_job_code: list[ApplicationSummary]


class CreateApplicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_id: int
    url: HttpUrlText = None
    job_title: str | None = Field(default=None, max_length=300)
    job_code: JobCode = None
    resume_document_name: str | None = None
    resume_revision_id: int | None = None
    metadata_document_name: str | None = None
    metadata_revision_id: int | None = None
    skill_document_name: str | None = None
    skill_revision_id: int | None = None
    resume_label: str | None = Field(default=None, max_length=300)
    initial_prompt_text: str | None = None
    job_description: str | None = None
    date_submitted: date | None = None
    manually_modified: bool = False
    modification_note: str | None = None
    source: str | None = Field(default=None, max_length=300)
    system: str | None = Field(default=None, max_length=300)
    confirm_create_duplicate: bool = False

    @model_validator(mode="after")
    def _check_document_ref_pairs(self) -> Self:
        DocumentRefRule.check(self)
        return self


class UpdateApplicationRequest(BaseModel):
    """Deliberately has no `status` field - `PATCH /applications/{id}` must
    422 on one, which `extra="forbid"` gives for free. See section 5.4."""

    model_config = ConfigDict(extra="forbid")

    company_id: int | None = None
    url: HttpUrlText = None
    job_title: str | None = Field(default=None, max_length=300)
    job_code: JobCode = None
    resume_document_name: str | None = None
    resume_revision_id: int | None = None
    metadata_document_name: str | None = None
    metadata_revision_id: int | None = None
    skill_document_name: str | None = None
    skill_revision_id: int | None = None
    resume_label: str | None = Field(default=None, max_length=300)
    initial_prompt_text: str | None = None
    job_description: str | None = None
    date_submitted: date | None = None
    manually_modified: bool | None = None
    modification_note: str | None = None
    source: str | None = Field(default=None, max_length=300)
    system: str | None = Field(default=None, max_length=300)
    confirm_create_duplicate: bool = False

    @model_validator(mode="after")
    def _check_document_ref_pairs(self) -> Self:
        DocumentRefRule.check(self)
        return self
