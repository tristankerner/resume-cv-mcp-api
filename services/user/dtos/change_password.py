from pydantic import BaseModel, SecretStr, field_validator, model_validator

from services.user.password_policy import PasswordPolicy


class ChangePasswordRequest(BaseModel):
    """Self-service only — see UserService.change_own_password. Unlike
    UpdateUserRequest, `current_password` is required here: an admin resetting
    a locked-out user's password legitimately does not know the current one,
    which is why that path stays on PATCH /users/{id} instead."""

    current_password: SecretStr
    new_password: SecretStr
    new_password_retype: SecretStr

    _validate_new_password = field_validator("new_password")(PasswordPolicy.validate)

    @model_validator(mode="after")
    def check_passwords(self) -> ChangePasswordRequest:
        if self.new_password != self.new_password_retype:
            raise ValueError("New passwords must match.")
        if self.new_password == self.current_password:
            raise ValueError(
                "New password must be different from the current password."
            )
        return self
