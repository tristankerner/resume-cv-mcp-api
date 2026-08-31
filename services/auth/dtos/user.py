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
    # What this user's roles grant, not what any particular credential of
    # theirs carries — a password JWT gets the full set, but an API key or an
    # OAuth token is narrowed further. Populated by UserService.get_current_user
    # via ScopeResolver.for_roles; there is no @Depends here because UserDto also
    # comes from from_attributes on a plain User row.
    scopes: list[Scopes] = []
    # Whether this account has any activated second factor. Here rather than
    # behind its own call because the client shows a "you have no MFA"
    # warning on every view, and a boolean the session already carries is
    # cheaper than a fetch per navigation. Populated by
    # UserService.get_current_user, like `scopes` above it.
    mfa_enrolled: bool = False

    model_config = ConfigDict(from_attributes=True)
