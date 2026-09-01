from pydantic import BaseModel, SecretStr, field_validator, model_validator

from services.user.password_policy import PasswordPolicy


class AdminResetPasswordRequest(BaseModel):
    """The admin path — see UserService.reset_password. No `current_password`:
    the point is recovering an account whose current password is exactly what
    is unknown or unusable, which is also why `PATCH /users/{id}` accepts a
    password with no current one to check. This route exists alongside it only
    to be a discoverable, dedicated way to do the one thing."""

    new_password: SecretStr
    new_password_retype: SecretStr

    _validate_new_password = field_validator("new_password")(PasswordPolicy.validate)

    @model_validator(mode="after")
    def check_passwords_match(self) -> AdminResetPasswordRequest:
        if self.new_password != self.new_password_retype:
            raise ValueError("New passwords must match.")
        return self
