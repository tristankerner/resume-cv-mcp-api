from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.auth.scopes import Scopes


class CreateApiKeyRequest(BaseModel):
    name: str = Field(min_length=1)  # what this key is for, e.g. "mcp-server"
    scopes: list[Scopes] = Field(min_length=1)
    # Omit to mint for yourself. Naming someone else requires users:admin.
    user_id: int | None = None
    expires_in_days: int | None = Field(default=None, gt=0)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Name must not be blank.")
        return stripped


class ApiKeyDto(BaseModel):
    """A key's metadata. Never carries the secret — that exists exactly once,
    in the response to the call that created it."""

    id: int
    user_id: int
    name: str
    prefix: str
    scopes: list[Scopes]
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class CreateApiKeyResponse(BaseModel):
    api_key: ApiKeyDto
    key: str  # shown once; it is not recoverable afterwards


class ListApiKeysResponse(BaseModel):
    data: list[ApiKeyDto]
