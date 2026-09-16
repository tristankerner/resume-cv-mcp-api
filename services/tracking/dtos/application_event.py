from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from persistence.application_event_contact import ApplicationEventContact
from services.common.datetimes import UtcDatetime
from services.tracking.enums import ApplicationStatus


class ContactIdRule:
    """Deduplication for `contact_ids`, carried by the annotated type rather
    than repeated as a validator on each request model.

    A class rather than a module-level function, for the reason
    `DocumentRefRule` in `dtos/application.py` states: `CLAUDE.md` allows
    neither a free-standing function nor a module-level helper here.

    Naming the same contact twice is a client bug with one obvious correct
    reading, and the join table's unique constraint would otherwise turn it
    into a 500. Order is first-seen, because the order contacts were named
    in is what `ApplicationEventContact` preserves.
    """

    @staticmethod
    def dedupe(value: list[int]) -> list[int]:
        seen: set[int] = set()
        deduped: list[int] = []
        for contact_id in value:
            if contact_id not in seen:
                seen.add(contact_id)
                deduped.append(contact_id)
        return deduped


ContactIds = Annotated[
    list[int],
    Field(max_length=ApplicationEventContact.MAX_PER_EVENT),
    AfterValidator(ContactIdRule.dedupe),
]


class EventContact(BaseModel):
    """One contact named on an event. `name` is the same
    "first last" join `contact_name` carried before, and is `None` when the
    contact has neither name part - the id is then the only identifier."""

    model_config = ConfigDict(extra="forbid")

    id: int
    name: str | None


class ApplicationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    application_id: int
    status: ApplicationStatus | None
    status_label: str | None
    contacts: list[EventContact]
    description: str | None
    rating: int | None
    occurred_at: UtcDatetime
    created_at: UtcDatetime


class CreateApplicationEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ApplicationStatus | None = None
    contact_ids: ContactIds = Field(default_factory=list)
    description: str | None = None
    rating: int | None = Field(default=None, ge=1, le=10)
    occurred_at: UtcDatetime | None = None


class UpdateApplicationEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ApplicationStatus | None = None
    contact_ids: ContactIds = Field(default_factory=list)
    description: str | None = None
    rating: int | None = Field(default=None, ge=1, le=10)
    occurred_at: UtcDatetime | None = None


class ApplicationEventWriteResponse(BaseModel):
    """The response for `POST`/`PATCH` on an event, and - unusually - for
    `DELETE` too: the client needs the application's recomputed status and a
    second round trip to get it would be silly. See API contract 14.5."""

    model_config = ConfigDict(extra="forbid")

    event: ApplicationEvent | None
    application_status: ApplicationStatus
    application_status_label: str
    application_status_changed_at: UtcDatetime | None
