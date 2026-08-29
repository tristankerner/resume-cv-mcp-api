from pydantic import BaseModel, SecretStr, field_validator, model_validator

from services.auth.roles import Roles
from services.user.password_policy import PasswordPolicy


class UpdateUserRequest(BaseModel):
    user_id: int | None = None
    username: str | None = None
    password: SecretStr | None = None
    password_retype: SecretStr | None = None
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    disabled: bool | None = None
    roles: list[Roles] | None = None

    @model_validator(mode="after")
    def check_at_least_one_field(self) -> UpdateUserRequest:
        # `is not None`, not truthiness: `disabled=False` and `roles=[]` are
        # both meaningful, provided values — an admin clearing every role, or
        # reactivating an account, must not be mistaken for an empty request.
        if not any(
            field is not None
            for field in (
                self.username,
                self.password,
                self.email,
                self.first_name,
                self.last_name,
                self.disabled,
                self.roles,
            )
        ):
            raise ValueError("At least one field must be provided.")

        if self.password is not None and self.password != self.password_retype:
            raise ValueError("Passwords must match.")
        return self

    _validate_password = field_validator("password")(PasswordPolicy.validate)
