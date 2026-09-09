from pydantic import BaseModel, ConfigDict, Field

from services.common.datetimes import UtcDatetime


class Contact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    company_id: int | None
    company_name: str | None
    first_name: str | None
    last_name: str | None
    email: str | None
    phone: str | None
    description: str | None
    personal_note: str | None
    rating: int | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class CreateContactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_id: int | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = Field(default=None, max_length=64)
    description: str | None = None
    personal_note: str | None = None
    rating: int | None = Field(default=None, ge=1, le=10)
    confirm_create_duplicate: bool = False


class UpdateContactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_id: int | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = Field(default=None, max_length=64)
    description: str | None = None
    personal_note: str | None = None
    rating: int | None = Field(default=None, ge=1, le=10)


class ContactOption(BaseModel):
    """One row of `GET /applications/{id}/contact-options` - a contact the
    client's event composer might plausibly want, reached by walking
    `company_relationships` out from the application's own company. See API
    contract 9.3."""

    model_config = ConfigDict(extra="forbid")

    id: int
    first_name: str | None
    last_name: str | None
    email: str | None
    phone: str | None
    rating: int | None
    # Both non-optional, unlike `Contact` above: a row only reaches this DTO
    # by having been found *through* a company in the relationship graph, so
    # a contact with no company - or one whose company row could not be
    # resolved - is skipped by the service rather than emitted with nulls
    # the client would render into a group heading.
    company_id: int
    company_name: str
    # 0 = the application's own company. 1..3 = hops away through
    # company_relationships, followed in both directions.
    depth: int
    # Human-readable chain, e.g. "via Acme Staffing → Globex". Empty at
    # depth 0.
    via: str | None
