from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from services.tracking.enums import ApplicationStatus


class ApplicationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    application_id: int
    status: ApplicationStatus | None
    status_label: str | None
    contact_id: int | None
    contact_name: str | None
    description: str | None
    rating: int | None
    occurred_at: datetime
    created_at: datetime


class CreateApplicationEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ApplicationStatus | None = None
    contact_id: int | None = None
    description: str | None = None
    rating: int | None = Field(default=None, ge=1, le=10)
    occurred_at: datetime | None = None


class UpdateApplicationEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ApplicationStatus | None = None
    contact_id: int | None = None
    description: str | None = None
    rating: int | None = Field(default=None, ge=1, le=10)
    occurred_at: datetime | None = None


class ApplicationEventWriteResponse(BaseModel):
    """The response for `POST`/`PATCH` on an event, and - unusually - for
    `DELETE` too: the client needs the application's recomputed status and a
    second round trip to get it would be silly. See API contract 14.5."""

    model_config = ConfigDict(extra="forbid")

    event: ApplicationEvent | None
    application_status: ApplicationStatus
    application_status_label: str
    application_status_changed_at: datetime | None
