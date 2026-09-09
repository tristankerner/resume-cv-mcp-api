from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    # An IANA name, or `null` to mean UTC. Not an account-recovery field —
    # see UserService.update_user — so it is deliberately absent from
    # `touches_recovery_fields` there.
    timezone: str | None = None

    @model_validator(mode="after")
    def check_at_least_one_field(self) -> UpdateUserRequest:
        # `is not None`, not truthiness: `disabled=False` and `roles=[]` are
        # both meaningful, provided values — an admin clearing every role, or
        # reactivating an account, must not be mistaken for an empty request.
        # `timezone` is checked by membership in `model_fields_set` instead,
        # since `null` is *also* a meaningful, explicit value for it (UTC),
        # and would otherwise be indistinguishable from "omitted".
        if "timezone" not in self.model_fields_set and not any(
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

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if not value:
            return None
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {value!r}") from exc
        return value
