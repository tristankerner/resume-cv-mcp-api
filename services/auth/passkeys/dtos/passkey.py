from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.common.datetimes import UtcDatetime


class PasskeyStatus(BaseModel):
    id: int
    label: str
    created_at: UtcDatetime
    last_used_at: UtcDatetime | None
    # "single_device" or "multi_device", straight off the BE/BS flags in the
    # authenticator data. The client turns this into "synced" vs "this device
    # only", which is the difference between "you have a backup" and "losing
    # this laptop loses this passkey".
    device_type: str
    backed_up: bool

    model_config = ConfigDict(from_attributes=True)


class PasskeyListResponse(BaseModel):
    """`enabled` is False when the deployment has no WEBAUTHN_RP_ID. The
    credential list is then always empty, and the client hides the whole
    section rather than offering a button that 404s."""

    enabled: bool
    credentials: list[PasskeyStatus]


class PasskeyRegistrationOptionsRequest(BaseModel):
    current_password: str


class PasskeyRegistrationOptionsResponse(BaseModel):
    # Passed to navigator.credentials.create() untouched. Typed as a dict and
    # not modelled: this is the W3C's schema, it changes on the W3C's
    # schedule, and re-declaring it here would be a second copy to keep in
    # step with py_webauthn's.
    options: dict
    registration_token: str
    expires_in: int


class PasskeyRegistrationRequest(BaseModel):
    registration_token: str
    label: str = Field(min_length=1, max_length=100)
    credential: dict

    @field_validator("label")
    @classmethod
    def strip_label(cls, value: str) -> str:
        # Same validator as EnrollTotpRequest.strip_label.
        stripped = value.strip()
        if not stripped:
            raise ValueError("Label must not be blank.")
        return stripped


class PasskeyRenameRequest(BaseModel):
    label: str = Field(min_length=1, max_length=100)

    @field_validator("label")
    @classmethod
    def strip_label(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Label must not be blank.")
        return stripped


class PasskeyPasswordConfirmRequest(BaseModel):
    """Deliberately not `mfa.dtos.mfa.PasswordConfirmRequest` re-imported:
    the coupling across package boundaries is worse than the two-line repeat,
    and that DTO's docstring explains itself in terms of MFA routes. See
    CLAUDE.md's DRY exception."""

    current_password: str


class PasskeyAuthenticationOptionsRequest(BaseModel):
    """`username` is optional: absent runs the discoverable-credential flow,
    where the authenticator names the account rather than the user typing
    it."""

    username: str | None = None


class PasskeyAuthenticationOptionsResponse(BaseModel):
    options: dict
    login_token: str
    expires_in: int


class PasskeyAuthenticationRequest(BaseModel):
    login_token: str
    credential: dict
