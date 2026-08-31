from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.auth.mfa.kinds import MfaMethodKind


class PasswordConfirmRequest(BaseModel):
    """Shared by every mutating MFA route — enrolment, regeneration and
    removal all require the current password (§7.2), so one small request
    body does for three routes rather than three near-identical ones."""

    current_password: str


class EnrollTotpRequest(BaseModel):
    label: str = Field(min_length=1)
    current_password: str

    @field_validator("label")
    @classmethod
    def strip_label(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Label must not be blank.")
        return stripped


class EnrollTotpResponse(BaseModel):
    """The secret and the otpauth URI exist here and nowhere else — no later
    read of MfaStatusResponse ever returns them again."""

    credential_id: int
    kind: MfaMethodKind
    label: str
    secret: str
    otpauth_uri: str


class ActivateTotpRequest(BaseModel):
    code: str


class BackupCodesResponse(BaseModel):
    """The plaintext codes exist here and nowhere else — the column only
    ever holds their hashes."""

    credential_id: int
    kind: MfaMethodKind
    codes: list[str]


class AvailableMfaMethod(BaseModel):
    kind: MfaMethodKind
    label: str
    allows_multiple: bool


class MfaCredentialStatus(BaseModel):
    id: int
    kind: MfaMethodKind
    label: str
    created_at: datetime
    activated_at: datetime | None
    last_used_at: datetime | None
    remaining: int | None

    model_config = ConfigDict(from_attributes=True)


class MfaStatusResponse(BaseModel):
    """Never a secret, an otpauth URI, or a backup code — those exist once,
    in the response that created them."""

    enrolled: bool
    credentials: list[MfaCredentialStatus]
    available_methods: list[AvailableMfaMethod]
    backup_codes_remaining: int | None
