from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from services.tracking.application_service import ApplicationService
from services.tracking.attachment_service import AttachmentService
from services.tracking.dtos.application import (
    ApplicationDetail,
    ApplicationSummary,
    CreateApplicationRequest,
    UpdateApplicationRequest,
)
from services.tracking.dtos.application_event import (
    ApplicationEvent,
    ApplicationEventWriteResponse,
    CreateApplicationEventRequest,
    UpdateApplicationEventRequest,
)
from services.tracking.dtos.attachment import (
    AttachmentDetail,
    AttachmentMeta,
    CreateAttachmentRequest,
)
from services.tracking.dtos.common import ListEnvelope
from services.tracking.dtos.contact import ContactOption
from services.tracking.enums import ApplicationStatus

ApplicationSort = Literal[
    "-date_submitted",
    "date_submitted",
    "company",
    "-company",
    "status",
    "-created_at",
    "created_at",
]
Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]
# Wider than the ordinary list `Limit`: capped at
# `CompanyRelationship.MAX_RELATED_COMPANIES` (200) companies rather than 200
# rows, and each company can hold several contacts.
ContactOptionsLimit = Annotated[int, Query(ge=1, le=500)]


class ApplicationsRouter:
    """Applications, their events and their attachments - see API contract
    14.5. No prefix: paths span `/applications`, `/application-events` and
    `/attachments`."""

    ApplicationServiceDep = Annotated[
        ApplicationService, Depends(ApplicationService.get_with_deps)
    ]
    AttachmentServiceDep = Annotated[
        AttachmentService, Depends(AttachmentService.get_with_deps)
    ]

    def __init__(self) -> None:
        self.router = APIRouter(tags=["tracking"])
        self._register()

    def _register(self) -> None:
        self.router.get("/applications")(self.list_applications)
        self.router.post("/applications", status_code=201)(self.create_application)
        # Registered before the parameterized single-application routes so
        # the more specific paths read first - see section 6.1.
        self.router.get("/applications/{application_id}/events")(self.list_events)
        self.router.post("/applications/{application_id}/events", status_code=201)(
            self.create_event
        )
        self.router.get("/applications/{application_id}/attachments")(
            self.list_attachments
        )
        self.router.post("/applications/{application_id}/attachments", status_code=201)(
            self.create_attachment
        )
        self.router.get("/applications/{application_id}/contact-options")(
            self.contact_options
        )
        self.router.get("/applications/{application_id}")(self.get_application)
        self.router.patch("/applications/{application_id}")(self.update_application)
        self.router.delete("/applications/{application_id}", status_code=204)(
            self.delete_application
        )
        self.router.patch("/application-events/{event_id}")(self.update_event)
        # 200, not 204: the response carries the recomputed application
        # status - see API contract 14.5.
        self.router.delete("/application-events/{event_id}")(self.delete_event)
        self.router.get("/attachments/{attachment_id}")(self.get_attachment)
        self.router.delete("/attachments/{attachment_id}", status_code=204)(
            self.delete_attachment
        )

    async def list_applications(
        self,
        application_service: ApplicationServiceDep,
        company_id: int | None = None,
        status: Annotated[list[ApplicationStatus] | None, Query()] = None,
        source: str | None = None,
        system: str | None = None,
        query: str | None = None,
        job_code: str | None = None,
        submitted_from: date | None = None,
        submitted_to: date | None = None,
        has_attachments: bool | None = None,
        limit: Limit = 50,
        offset: Offset = 0,
        sort: ApplicationSort = "-date_submitted",
    ) -> ListEnvelope[ApplicationSummary]:
        return await application_service.list_applications(
            company_id=company_id,
            statuses=status,
            source=source,
            system=system,
            query=query,
            job_code=job_code,
            submitted_from=submitted_from,
            submitted_to=submitted_to,
            has_attachments=has_attachments,
            sort=sort,
            limit=limit,
            offset=offset,
        )

    async def create_application(
        self,
        request: CreateApplicationRequest,
        application_service: ApplicationServiceDep,
    ) -> ApplicationSummary:
        return await application_service.create_application(request)

    async def get_application(
        self, application_id: int, application_service: ApplicationServiceDep
    ) -> ApplicationDetail:
        return await application_service.get_application(application_id)

    async def update_application(
        self,
        application_id: int,
        request: UpdateApplicationRequest,
        application_service: ApplicationServiceDep,
    ) -> ApplicationSummary:
        return await application_service.update_application(application_id, request)

    async def delete_application(
        self, application_id: int, application_service: ApplicationServiceDep
    ) -> None:
        await application_service.delete_application(application_id)

    async def list_events(
        self, application_id: int, application_service: ApplicationServiceDep
    ) -> ListEnvelope[ApplicationEvent]:
        return await application_service.list_events(application_id)

    async def create_event(
        self,
        application_id: int,
        request: CreateApplicationEventRequest,
        application_service: ApplicationServiceDep,
    ) -> ApplicationEventWriteResponse:
        return await application_service.create_event(application_id, request)

    async def update_event(
        self,
        event_id: int,
        request: UpdateApplicationEventRequest,
        application_service: ApplicationServiceDep,
    ) -> ApplicationEventWriteResponse:
        return await application_service.update_event(event_id, request)

    async def delete_event(
        self, event_id: int, application_service: ApplicationServiceDep
    ) -> ApplicationEventWriteResponse:
        return await application_service.delete_event(event_id)

    async def list_attachments(
        self, application_id: int, attachment_service: AttachmentServiceDep
    ) -> ListEnvelope[AttachmentMeta]:
        return await attachment_service.list_attachments(application_id)

    async def create_attachment(
        self,
        application_id: int,
        request: CreateAttachmentRequest,
        attachment_service: AttachmentServiceDep,
    ) -> AttachmentMeta:
        return await attachment_service.create_attachment(application_id, request)

    async def contact_options(
        self,
        application_id: int,
        application_service: ApplicationServiceDep,
        query: str | None = None,
        limit: ContactOptionsLimit = 200,
    ) -> ListEnvelope[ContactOption]:
        return await application_service.contact_options(application_id, query, limit)

    async def get_attachment(
        self, attachment_id: int, attachment_service: AttachmentServiceDep
    ) -> AttachmentDetail:
        return await attachment_service.get_attachment(attachment_id)

    async def delete_attachment(
        self, attachment_id: int, attachment_service: AttachmentServiceDep
    ) -> None:
        await attachment_service.delete_attachment(attachment_id)
