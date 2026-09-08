from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


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
    created_at: datetime
    updated_at: datetime


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
