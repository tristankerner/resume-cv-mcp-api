from pydantic import BaseModel, ConfigDict, Field

from services.common.datetimes import UtcDatetime
from services.tracking.dtos.application import ApplicationSummary
from services.tracking.dtos.common import HttpUrlText
from services.tracking.dtos.contact import Contact
from services.tracking.enums import CompanyRelationshipType, StackItemType

# Same restriction, and the same reason, as `HttpUrlText` - see its
# docstring in dtos/common.py.
Website = HttpUrlText


class CompanyRelationshipDto(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    from_company_id: int
    from_company_name: str
    to_company_id: int
    to_company_name: str
    type: CompanyRelationshipType
    note: str | None
    created_at: UtcDatetime


class CompanyStackItemDto(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    company_id: int
    name: str
    type: StackItemType
    description: str | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class CompanySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    website: str | None
    description: str | None
    personal_note: str | None
    application_count: int
    contact_count: int
    created_at: UtcDatetime
    updated_at: UtcDatetime


class CompanyDetail(CompanySummary):
    relationships: list[CompanyRelationshipDto]
    stack: list[CompanyStackItemDto]
    contacts: list[Contact]
    recent_applications: list[ApplicationSummary]


class CreateCompanyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    website: Website = None
    description: str | None = None
    personal_note: str | None = None
    confirm_create_duplicate: bool = False


class UpdateCompanyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    website: Website = None
    description: str | None = None
    personal_note: str | None = None
    confirm_create_duplicate: bool = False


class CreateCompanyRelationshipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_company_id: int
    type: CompanyRelationshipType
    note: str | None = None


class UpdateCompanyRelationshipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: CompanyRelationshipType | None = None
    note: str | None = None


class CreateCompanyStackItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    type: StackItemType
    description: str | None = None
    confirm_create_duplicate: bool = False


class UpdateCompanyStackItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    type: StackItemType | None = None
    description: str | None = None
    confirm_create_duplicate: bool = False
