from pydantic import BaseModel, ConfigDict

from services.auth.roles import Roles
from services.common.datetimes import UtcDatetime


class AdminUserDto(BaseModel):
    """One row of `GET /users` — everything the admin UI needs to render the
    unlock / reset-password / reset-mfa / deactivate actions without a call
    per user. `locked` and `mfa_enrolled` are not columns; UserService.list_users
    populates them after `model_validate`, the same pattern UserDto uses for
    `scopes`."""

    id: int
    username: str
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    active: bool
    roles: list[Roles]
    timezone: str | None = None
    failed_login_count: int
    locked_until: UtcDatetime | None = None
    locked_permanently_at: UtcDatetime | None = None
    mfa_enrolled: bool = False
    locked: bool = False

    model_config = ConfigDict(from_attributes=True)


class ListUsersResponse(BaseModel):
    data: list[AdminUserDto]
