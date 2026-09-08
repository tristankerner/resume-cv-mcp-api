from persistence.application import Application
from services.tracking.dtos.application import ApplicationDetail, ApplicationSummary
from services.tracking.dtos.application_event import (
    ApplicationEvent as ApplicationEventDto,
)
from services.tracking.dtos.attachment import AttachmentMeta
from services.tracking.dtos.common import DocumentRef
from services.tracking.enums import APPLICATION_STATUS_LABELS, ApplicationStatus


class ApplicationView:
    """Builds the wire DTOs for an `Application` row - shared by
    `CompanyService` (the `recent_applications` preview) and
    `ApplicationService` (the applications endpoints themselves), so the two
    surfaces cannot drift on what a summary contains."""

    @staticmethod
    def summary(
        application: Application,
        company_name: str,
        event_count: int,
        attachment_count: int,
        job_code_match_count: int = 0,
    ) -> ApplicationSummary:
        status = ApplicationStatus(application.status)
        return ApplicationSummary(
            id=application.id,
            company_id=application.company_id,
            company_name=company_name,
            job_title=application.job_title,
            job_code=application.job_code,
            job_code_match_count=job_code_match_count,
            url=application.url,
            source=application.source,
            system=application.system,
            status=status,
            status_label=APPLICATION_STATUS_LABELS[status],
            status_changed_at=application.status_changed_at,
            date_submitted=application.date_submitted,
            manually_modified=application.manually_modified,
            resume_label=application.resume_label,
            event_count=event_count,
            attachment_count=attachment_count,
            created_at=application.created_at,
            updated_at=application.updated_at,
        )

    @staticmethod
    def _document_ref(name: str | None, revision_id: int | None) -> DocumentRef | None:
        if name is None or revision_id is None:
            return None
        return DocumentRef(name=name, revision_id=revision_id)

    @staticmethod
    def detail(
        application: Application,
        company_name: str,
        events: list[ApplicationEventDto],
        attachments: list[AttachmentMeta],
        related_by_job_code: list[ApplicationSummary] | None = None,
    ) -> ApplicationDetail:
        related = related_by_job_code or []
        summary = ApplicationView.summary(
            application, company_name, len(events), len(attachments), len(related)
        )
        return ApplicationDetail(
            **summary.model_dump(),
            job_description=application.job_description,
            initial_prompt_text=application.initial_prompt_text,
            modification_note=application.modification_note,
            resume_document=ApplicationView._document_ref(
                application.resume_document_name, application.resume_revision_id
            ),
            metadata_document=ApplicationView._document_ref(
                application.metadata_document_name, application.metadata_revision_id
            ),
            skill_document=ApplicationView._document_ref(
                application.skill_document_name, application.skill_revision_id
            ),
            events=events,
            attachments=attachments,
            related_by_job_code=related,
        )
