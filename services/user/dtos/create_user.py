from pydantic import BaseModel, SecretStr, field_validator

from services.auth.roles import Roles
from services.user.password_policy import PasswordPolicy


class CreateUserRequest(BaseModel):
    username: str
    password: SecretStr
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    disabled: bool | None = None
    roles: list[Roles] = []

    _validate_password = field_validator("password")(PasswordPolicy.validate)


class CreateUserResponse(BaseModel):
    id: int
