from datetime import date
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.application import Application, ApplicationSearchRow
from persistence.application_attachment import ApplicationAttachment
from persistence.application_event import ApplicationEvent
from persistence.base import Clock
from persistence.company import Company
from persistence.contact import Contact
from persistence.document import Document
from persistence.lookup import Lookup
from services.auth.auth_service import AuthService
from services.auth.principal import Principal
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService
from services.document.document_types import DocumentType
from services.tracking.application_view import ApplicationView
from services.tracking.dtos.application import (
    ApplicationDetail,
    ApplicationSummary,
    CreateApplicationRequest,
    UpdateApplicationRequest,
)
from services.tracking.dtos.application_event import (
    ApplicationEvent as ApplicationEventDto,
)
from services.tracking.dtos.application_event import (
    ApplicationEventWriteResponse,
    CreateApplicationEventRequest,
    UpdateApplicationEventRequest,
)
from services.tracking.dtos.attachment import AttachmentMeta
from services.tracking.dtos.common import DuplicateCandidate, ListEnvelope
from services.tracking.duplicates import DuplicateFinder
from services.tracking.enums import APPLICATION_STATUS_LABELS, ApplicationStatus
from services.tracking.exceptions import TrackingErrors
from services.tracking.normalization import Normalizer
from services.tracking.tracking_service import TrackingServiceBase

DOCUMENT_SLOTS = (
    ("resume_document_name", "resume_revision_id", DocumentType.RESUME),
    ("metadata_document_name", "metadata_revision_id", DocumentType.METADATA),
    ("skill_document_name", "skill_revision_id", DocumentType.SKILL),
)


class ApplicationService(TrackingServiceBase):
    """Applications and their events. See sections 5.3, 5.4, 5.6 and 14.5 of
    the tracking plan."""

    async def _require_application(self, application_id: int) -> Application:
        application = await Application.get(self.db, self._owner(), application_id)
        if application is None:
            raise TrackingErrors.not_found("Application")
        return application

    async def _validate_company(self, company_id: int) -> Company:
        company = await Company.get(self.db, self._owner(), company_id)
        if company is None:
            raise TrackingErrors.invalid_reference("company_id", "company")
        return company

    async def _validate_document_refs(
        self,
        name_by_field: dict[str, str | None],
        revision_by_field: dict[str, int | None],
    ) -> None:
        owner = self._owner()
        for name_field, revision_field, expected_type in DOCUMENT_SLOTS:
            name = name_by_field[name_field]
            revision_id = revision_by_field[revision_field]
            # The DTO's `_check_document_ref_pairs` validator guarantees this
            # pair is either both set or both None.
            if name is None or revision_id is None:
                continue
            document = await Document.get_revision(self.db, owner, name, revision_id)
            if document is None:
                raise TrackingErrors.invalid_reference(name_field, "document")
            if document.type != expected_type.value:
                raise TrackingErrors.document_type_mismatch(
                    name_field, expected_type.value, document.type
                )

    async def _candidates(
        self,
        company_id: int,
        job_title: str | None,
        date_submitted: date | None,
        exclude_id: int | None,
    ) -> list[DuplicateCandidate]:
        normalized_title = Normalizer.job_title(job_title)
        if normalized_title is None:
            return []
        owner = self._owner()
        rows = await Application.candidates_for_dedup(
            self.db, owner, company_id, normalized_title
        )
        dates_by_id = {row[0]: row[3] for row in rows}
        haystack = [(row[0], row[1], row[2]) for row in rows if row[0] != exclude_id]
        candidates = DuplicateFinder.rank(normalized_title, haystack)

        def _matches_date(candidate_id: int) -> bool:
            other_date = dates_by_id.get(candidate_id)
            if date_submitted is None or other_date is None:
                return True
            return abs((date_submitted - other_date).days) <= 14

        return [candidate for candidate in candidates if _matches_date(candidate.id)]

    def _refuse_if_duplicate(
        self, candidates: list[DuplicateCandidate], by_job_code: bool = False
    ) -> None:
        if not candidates:
            return
        if by_job_code:
            message = (
                f"{len(candidates)} application(s) already carry this job code, "
                "so this is the same job reaching you more than once. Use one of "
                "them, or resend with confirm_create_duplicate: true to record "
                "this submission separately."
            )
        else:
            message = (
                f"{len(candidates)} application(s) to this company already look "
                "like this one. Use one of them, or resend with "
                "confirm_create_duplicate: true."
            )
        raise TrackingErrors.duplicate("duplicate_application", message, candidates)

    async def _company_name(self, company_id: int) -> str:
        company = await Company.get(self.db, self._owner(), company_id)
        return company.name if company is not None else ""

    async def _event_dtos(
        self, events: list[ApplicationEvent]
    ) -> list[ApplicationEventDto]:
        """DTOs for a page of events in one round trip for every contact
        name, rather than one `Contact.get` per row."""
        if not events:
            return []
        contact_ids = [
            event.contact_id for event in events if event.contact_id is not None
        ]
        names = await Lookup.map(
            self.db,
            key_column=Contact.id,
            value_columns=(Contact.first_name, Contact.last_name),
            owner_column=Contact.user_id,
            owner_id=self._owner(),
            keys=contact_ids,
        )

        def _contact_name(contact_id: int | None) -> str | None:
            if contact_id is None or contact_id not in names:
                return None
            row = names[contact_id]
            return (
                " ".join(
                    part for part in (row.first_name, row.last_name) if part
                ).strip()
                or None
            )

        result = []
        for event in events:
            status = (
                ApplicationStatus(event.status) if event.status is not None else None
            )
            result.append(
                ApplicationEventDto(
                    id=event.id,
                    application_id=event.application_id,
                    status=status,
                    status_label=APPLICATION_STATUS_LABELS[status] if status else None,
                    contact_id=event.contact_id,
                    contact_name=_contact_name(event.contact_id),
                    description=event.description,
                    rating=event.rating,
                    occurred_at=event.occurred_at,
                    created_at=event.created_at,
                )
            )
        return result

    async def _event_dto(self, event: ApplicationEvent) -> ApplicationEventDto:
        return (await self._event_dtos([event]))[0]

    async def _attachment_dto(
        self, attachment: ApplicationAttachment
    ) -> AttachmentMeta:
        return AttachmentMeta(
            id=attachment.id,
            application_id=attachment.application_id,
            kind=attachment.kind,
            filename=attachment.filename,
            content_type=attachment.content_type,
            byte_size=attachment.byte_size,
            sha256=attachment.sha256,
            created_at=attachment.created_at,
        )

    async def _to_summaries(
        self, applications: list[Application]
    ) -> list[ApplicationSummary]:
        """Summaries for a page of applications in a fixed number of queries.

        Three grouped lookups for the whole page rather than three per row:
        `limit` allows 200 applications, and a per-row company name, event
        count and attachment count is 600 round trips for one list request.
        """
        if not applications:
            return []
        owner = self._owner()
        application_ids = [application.id for application in applications]
        company_ids = list({application.company_id for application in applications})

        names = await Company.names_for(self.db, owner, company_ids)
        event_counts = await ApplicationEvent.counts_for(
            self.db, owner, application_ids
        )
        attachment_counts = await ApplicationAttachment.counts_for(
            self.db, owner, application_ids
        )
        job_code_totals = await Application.job_code_match_counts(
            self.db,
            owner,
            [
                application.normalized_job_code
                for application in applications
                if application.normalized_job_code
            ],
        )
        return [
            ApplicationView.summary(
                application,
                names.get(application.company_id, ""),
                event_counts.get(application.id, 0),
                attachment_counts.get(application.id, 0),
                # Minus one for the row itself: the field answers "how many
                # *others* share this code".
                max(
                    job_code_totals.get(application.normalized_job_code or "", 0) - 1, 0
                ),
            )
            for application in applications
        ]

    async def _to_summary(self, application: Application) -> ApplicationSummary:
        return (await self._to_summaries([application]))[0]

    async def _summaries_from_search_rows(
        self, rows: list[ApplicationSearchRow]
    ) -> list[ApplicationSummary]:
        """Summaries for a page `Application.search` already produced: the
        company name and per-row counts rode along as correlated columns, so
        only `job_code_match_count` still needs a grouped follow-up - and
        that follow-up is skipped entirely when nothing on the page carries
        a job code."""
        if not rows:
            return []
        owner = self._owner()
        job_code_totals = await Application.job_code_match_counts(
            self.db,
            owner,
            [
                row.application.normalized_job_code
                for row in rows
                if row.application.normalized_job_code
            ],
        )
        return [
            ApplicationView.summary(
                row.application,
                row.company_name,
                row.event_count,
                row.attachment_count,
                # Minus one for the row itself: the field answers "how many
                # *others* share this code".
                max(
                    job_code_totals.get(row.application.normalized_job_code or "", 0)
                    - 1,
                    0,
                ),
            )
            for row in rows
        ]

    async def _related_by_job_code(
        self, application: Application
    ) -> list[ApplicationSummary]:
        """This owner's other applications carrying the same job code.

        Across companies on purpose - see `Application.sharing_job_code`. This
        is the answer to "have two recruiters put me forward for the same
        requisition?", which is a question about the code, not the company.
        """
        if not application.normalized_job_code:
            return []
        rows = await Application.sharing_job_code(
            self.db, self._owner(), application.normalized_job_code, application.id
        )
        return await self._to_summaries(rows)

    async def _job_code_candidates(
        self, job_code: str | None, exclude_id: int | None
    ) -> list[DuplicateCandidate]:
        """A shared job code is an exact match, not a fuzzy one, and it
        outranks the company/title/date heuristic: two rows carrying the same
        requisition code are the same job even when the companies differ,
        which is exactly what happens when two agencies submit you."""
        normalized = Normalizer.job_code(job_code)
        if normalized is None:
            return []
        owner = self._owner()
        rows = await Application.sharing_job_code(
            self.db, owner, normalized, exclude_id
        )
        if not rows:
            return []
        names = await Company.names_for(
            self.db, owner, [row.company_id for row in rows]
        )
        return [
            DuplicateCandidate(
                id=row.id,
                label=row.job_title or row.job_code or f"application {row.id}",
                match="exact",
                score=1.0,
                hint=self._job_code_hint(row, names.get(row.company_id, "")),
            )
            for row in rows
        ]

    @staticmethod
    def _job_code_hint(row: Application, company_name: str) -> str:
        parts = [f"job code {row.job_code}"] if row.job_code else []
        if company_name:
            parts.append(f"via {company_name}")
        if row.source:
            parts.append(f"from {row.source}")
        if row.date_submitted:
            parts.append(f"on {row.date_submitted.isoformat()}")
        return ", ".join(parts)

    async def _recompute_status(self, application: Application) -> None:
        """`status` is the status of the most recent event that states one -
        ordered by `occurred_at` descending, `id` descending as the tiebreak.
        `submitted` when no event states a status."""
        latest = await ApplicationEvent.latest_with_status(
            self.db, self._owner(), application.id
        )
        if latest is None:
            application.status = ApplicationStatus.SUBMITTED.value
            application.status_changed_at = None
        elif latest.status is not None:
            # `latest_with_status` filters on `status IS NOT NULL`, so this
            # branch always runs when `latest` does - the `elif` only avoids
            # narrowing `latest.status` with an assertion.
            application.status = latest.status
            application.status_changed_at = latest.occurred_at
        application.updated_at = Clock.utcnow()

    async def list_applications(
        self,
        *,
        company_id: int | None,
        statuses: list[ApplicationStatus] | None,
        source: str | None,
        system: str | None,
        query: str | None,
        job_code: str | None,
        submitted_from: date | None,
        submitted_to: date | None,
        has_attachments: bool | None,
        sort: str,
        limit: int,
        offset: int,
    ) -> ListEnvelope[ApplicationSummary]:
        self._require(Scopes.APPLICATIONS_READ)
        rows, total = await Application.search(
            self.db,
            self._owner(),
            company_id=company_id,
            statuses=[status.value for status in statuses] if statuses else None,
            source=source,
            system=system,
            query=query,
            job_code=Normalizer.job_code_key(job_code),
            submitted_from=submitted_from,
            submitted_to=submitted_to,
            has_attachments=has_attachments,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        data = await self._summaries_from_search_rows(list(rows))
        return ListEnvelope(data=data, total=total, limit=limit, offset=offset)

    async def get_application(self, application_id: int) -> ApplicationDetail:
        self._require(Scopes.APPLICATIONS_READ)
        owner = self._owner()
        application = await self._require_application(application_id)
        company_name = await self._company_name(application.company_id)
        events = await self._event_dtos(
            await ApplicationEvent.list_for_application(self.db, owner, application_id)
        )
        attachments = [
            await self._attachment_dto(attachment)
            for attachment in await ApplicationAttachment.list_metadata_for_application(
                self.db, owner, application_id
            )
        ]
        related = await self._related_by_job_code(application)
        return ApplicationView.detail(
            application, company_name, events, attachments, related
        )

    async def create_application(
        self, request: CreateApplicationRequest
    ) -> ApplicationSummary:
        self._require(Scopes.APPLICATIONS_WRITE)
        await self._begin_write()
        owner = self._owner()

        await self._validate_company(request.company_id)
        await self._validate_document_refs(
            {
                "resume_document_name": request.resume_document_name,
                "metadata_document_name": request.metadata_document_name,
                "skill_document_name": request.skill_document_name,
            },
            {
                "resume_revision_id": request.resume_revision_id,
                "metadata_revision_id": request.metadata_revision_id,
                "skill_revision_id": request.skill_revision_id,
            },
        )

        if not request.confirm_create_duplicate:
            # Job code first: it is an exact identifier, so when it matches
            # there is no point reporting a fuzzy title match as well.
            candidates = await self._job_code_candidates(request.job_code, None)
            if candidates:
                self._refuse_if_duplicate(candidates, by_job_code=True)
            candidates = await self._candidates(
                request.company_id, request.job_title, request.date_submitted, None
            )
            self._refuse_if_duplicate(candidates)

        application = Application(
            user_id=owner,
            company_id=request.company_id,
            url=request.url,
            job_title=request.job_title,
            normalized_job_title=Normalizer.job_title(request.job_title),
            job_code=request.job_code,
            normalized_job_code=Normalizer.job_code(request.job_code),
            resume_document_name=request.resume_document_name,
            resume_revision_id=request.resume_revision_id,
            metadata_document_name=request.metadata_document_name,
            metadata_revision_id=request.metadata_revision_id,
            skill_document_name=request.skill_document_name,
            skill_revision_id=request.skill_revision_id,
            resume_label=request.resume_label,
            initial_prompt_text=request.initial_prompt_text,
            job_description=request.job_description,
            date_submitted=request.date_submitted,
            manually_modified=request.manually_modified,
            modification_note=request.modification_note,
            source=request.source,
            system=request.system,
            status=ApplicationStatus.SUBMITTED.value,
        )
        self.db.add(application)
        await self.db.commit()
        return await self._to_summary(application)

    async def update_application(
        self, application_id: int, request: UpdateApplicationRequest
    ) -> ApplicationSummary:
        self._require(Scopes.APPLICATIONS_WRITE)
        await self._begin_write()
        application = await self._require_application(application_id)
        fields = request.model_fields_set

        if "company_id" in fields and request.company_id is not None:
            await self._validate_company(request.company_id)
            application.company_id = request.company_id

        name_by_field = {
            "resume_document_name": application.resume_document_name,
            "metadata_document_name": application.metadata_document_name,
            "skill_document_name": application.skill_document_name,
        }
        revision_by_field = {
            "resume_revision_id": application.resume_revision_id,
            "metadata_revision_id": application.metadata_revision_id,
            "skill_revision_id": application.skill_revision_id,
        }
        touched_refs = False
        for name_field, revision_field, _ in DOCUMENT_SLOTS:
            if name_field in fields:
                name_by_field[name_field] = getattr(request, name_field)
                revision_by_field[revision_field] = getattr(request, revision_field)
                touched_refs = True
        if touched_refs:
            await self._validate_document_refs(name_by_field, revision_by_field)
            for name_field, revision_field, _ in DOCUMENT_SLOTS:
                setattr(application, name_field, name_by_field[name_field])
                setattr(application, revision_field, revision_by_field[revision_field])

        if "url" in fields:
            application.url = request.url
        if "job_title" in fields:
            application.job_title = request.job_title
            application.normalized_job_title = Normalizer.job_title(request.job_title)
        if "job_code" in fields:
            application.job_code = request.job_code
            application.normalized_job_code = Normalizer.job_code(request.job_code)
        if "resume_label" in fields:
            application.resume_label = request.resume_label
        if "initial_prompt_text" in fields:
            application.initial_prompt_text = request.initial_prompt_text
        if "job_description" in fields:
            application.job_description = request.job_description
        if "date_submitted" in fields:
            application.date_submitted = request.date_submitted
        if "manually_modified" in fields and request.manually_modified is not None:
            application.manually_modified = request.manually_modified
        if "modification_note" in fields:
            application.modification_note = request.modification_note
        if "source" in fields:
            application.source = request.source
        if "system" in fields:
            application.system = request.system

        if self.db.is_modified(application, include_collections=False):
            application.updated_at = Clock.utcnow()
        await self.db.commit()
        return await self._to_summary(application)

    async def delete_application(self, application_id: int) -> None:
        self._require(Scopes.APPLICATIONS_DELETE)
        await self._begin_write()
        owner = self._owner()
        application = await self._require_application(application_id)

        for event in await ApplicationEvent.list_for_application(
            self.db, owner, application_id
        ):
            await self.db.delete(event)
        for attachment in await ApplicationAttachment.list_metadata_for_application(
            self.db, owner, application_id
        ):
            await self.db.delete(attachment)
        await self.db.delete(application)
        await self.db.commit()

    async def list_events(
        self, application_id: int
    ) -> ListEnvelope[ApplicationEventDto]:
        self._require(Scopes.APPLICATIONS_READ)
        owner = self._owner()
        await self._require_application(application_id)
        rows = await ApplicationEvent.list_for_application(
            self.db, owner, application_id
        )
        data = await self._event_dtos(rows)
        return ListEnvelope(data=data, total=len(data), limit=len(data), offset=0)

    async def _event_write_response(
        self, event: ApplicationEvent | None, application: Application
    ) -> ApplicationEventWriteResponse:
        status = ApplicationStatus(application.status)
        return ApplicationEventWriteResponse(
            event=await self._event_dto(event) if event is not None else None,
            application_status=status,
            application_status_label=APPLICATION_STATUS_LABELS[status],
            application_status_changed_at=application.status_changed_at,
        )

    async def create_event(
        self, application_id: int, request: CreateApplicationEventRequest
    ) -> ApplicationEventWriteResponse:
        self._require(Scopes.APPLICATIONS_WRITE)
        await self._begin_write()
        owner = self._owner()
        application = await self._require_application(application_id)

        if not any((request.status, request.description, request.rating)):
            raise TrackingErrors.event_needs_content()
        if request.contact_id is not None:
            contact = await Contact.get(self.db, owner, request.contact_id)
            if contact is None:
                raise TrackingErrors.invalid_reference("contact_id", "contact")

        event = ApplicationEvent(
            user_id=owner,
            application_id=application_id,
            status=request.status.value if request.status else None,
            contact_id=request.contact_id,
            description=request.description,
            rating=request.rating,
            occurred_at=request.occurred_at or Clock.utcnow(),
        )
        self.db.add(event)
        await self.db.flush()
        await self._recompute_status(application)
        await self.db.commit()
        return await self._event_write_response(event, application)

    async def _require_event(self, event_id: int) -> ApplicationEvent:
        event = await ApplicationEvent.get(self.db, self._owner(), event_id)
        if event is None:
            raise TrackingErrors.not_found("Application event")
        return event

    async def update_event(
        self, event_id: int, request: UpdateApplicationEventRequest
    ) -> ApplicationEventWriteResponse:
        self._require(Scopes.APPLICATIONS_WRITE)
        await self._begin_write()
        owner = self._owner()
        event = await self._require_event(event_id)
        application = await self._require_application(event.application_id)
        fields = request.model_fields_set

        if "status" in fields:
            event.status = request.status.value if request.status else None
        if "contact_id" in fields:
            if request.contact_id is not None:
                contact = await Contact.get(self.db, owner, request.contact_id)
                if contact is None:
                    raise TrackingErrors.invalid_reference("contact_id", "contact")
            event.contact_id = request.contact_id
        if "description" in fields:
            event.description = request.description
        if "rating" in fields:
            event.rating = request.rating
        if "occurred_at" in fields and request.occurred_at is not None:
            event.occurred_at = request.occurred_at

        await self.db.flush()
        await self._recompute_status(application)
        await self.db.commit()
        return await self._event_write_response(event, application)

    async def delete_event(self, event_id: int) -> ApplicationEventWriteResponse:
        self._require(Scopes.APPLICATIONS_DELETE)
        await self._begin_write()
        event = await self._require_event(event_id)
        application = await self._require_application(event.application_id)

        await self.db.delete(event)
        await self.db.flush()
        await self._recompute_status(application)
        await self.db.commit()
        return await self._event_write_response(None, application)

    @staticmethod
    def get_with_deps(
        db: Annotated[AsyncSession, Depends(DatabaseService.get_async_db_session)],
        principal: Annotated[
            Principal | None, Depends(AuthService.get_optional_principal)
        ],
    ) -> ApplicationService:
        return ApplicationService(db, principal)
