"""Wire shapes for the new endpoints. Named after the RFC that defines each
one, since that is the vocabulary a client implementation was written
against."""

from pydantic import BaseModel, Field, field_validator


class AuthorizationServerMetadata(BaseModel):
    """RFC 8414. Served at the origin root — clients derive it from the
    issuer, never from a mounted path."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    # Absent, not null, when registration is closed: RFC 8414 marks it
    # OPTIONAL, and a JSON null here is a malformed metadata document. The
    # route serving this sets response_model_exclude_none.
    registration_endpoint: str | None = None
    revocation_endpoint: str
    scopes_supported: list[str]
    response_types_supported: list[str] = ["code"]
    grant_types_supported: list[str] = ["authorization_code", "refresh_token"]
    code_challenge_methods_supported: list[str] = ["S256"]
    token_endpoint_auth_methods_supported: list[str] = ["client_secret_post", "none"]


# "none" is the expected shape for an MCP client: it cannot keep a secret, so
# PKCE binds the exchange instead. The other methods RFC 7591 defines are
# refused as unsupported rather than silently downgraded.
SUPPORTED_AUTH_METHODS = frozenset({"none", "client_secret_post"})


class ClientRegistrationRequest(BaseModel):
    """RFC 7591. Every field but `redirect_uris` is free text the registrant
    asserts about itself, which is why only `redirect_uris` is validated
    against anything — see services/oauth/redirect_allowlist.py."""

    redirect_uris: list[str] = Field(min_length=1)
    client_name: str | None = None
    grant_types: list[str] = ["authorization_code", "refresh_token"]
    response_types: list[str] = ["code"]
    token_endpoint_auth_method: str = "none"
    scope: str | None = None
    # Accepted and ignored: asserted metadata with no bearing on anything
    # this server checks.
    client_uri: str | None = None
    logo_uri: str | None = None
    tos_uri: str | None = None

    @field_validator("token_endpoint_auth_method")
    @classmethod
    def known_auth_method(cls, value: str) -> str:
        if value not in SUPPORTED_AUTH_METHODS:
            raise ValueError(
                f"Unsupported token_endpoint_auth_method: {value!r}. "
                f"Use one of {sorted(SUPPORTED_AUTH_METHODS)}."
            )
        return value


class ClientRegistrationResponse(BaseModel):
    client_id: str
    client_secret: str | None = None
    client_id_issued_at: int
    client_secret_expires_at: int = 0  # 0: RFC 7591's spelling of "never"
    redirect_uris: list[str]
    client_name: str | None = None
    grant_types: list[str]
    response_types: list[str]
    token_endpoint_auth_method: str
    scope: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    refresh_token: str | None = None
    scope: str
