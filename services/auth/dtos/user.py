from pydantic import BaseModel, ConfigDict

from services.auth.roles import Roles
from services.auth.scopes import Scopes


class UserDto(BaseModel):
    id: int
    username: str
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    active: bool | None = None
    roles: list[Roles] = []
    # IANA name, or `None` for UTC. Display-only — the API never renders
    # local time; the client converts. See services/common/datetimes.py.
    timezone: str | None = None
    # What this user's roles grant, not what any particular credential of
    # theirs carries — an API key or OAuth token is narrowed further. Populated
    # by UserService.get_current_user, since UserDto also comes from
    # from_attributes on a plain User row.
    scopes: list[Scopes] = []
    # Carried here rather than behind its own call because the client shows a
    # "you have no MFA" warning on every view. Populated by
    # UserService.get_current_user, like `scopes` above.
    mfa_enrolled: bool = False

    model_config = ConfigDict(from_attributes=True)
