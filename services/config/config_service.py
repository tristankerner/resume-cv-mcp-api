import os
from enum import StrEnum, auto
from typing import Annotated, ClassVar

from cryptography.fernet import Fernet
from pydantic import (
    AnyHttpUrl,
    AnyUrl,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from services.oauth.redirect_allowlist import RedirectAllowlist
from services.service_interface import ServiceProviderInterface


class UnexpectedNoSettings(Exception):
    pass


class Environment(StrEnum):
    """Which deployment this process is.

    Only used to decide what to lock down. Development is the default, so a
    production deployment has to say so explicitly.
    """

    DEVELOPMENT = auto()
    PRODUCTION = auto()


class ConfigServiceModel(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # With SECRETS_DIR set, any setting below may come from a Docker secret
        # file instead of an environment variable — env vars are readable via
        # `docker inspect` and leak into child processes.
        secrets_dir=os.environ.get("SECRETS_DIR"),
    )

    environment: Environment = Field(
        default=Environment.DEVELOPMENT, alias="ENVIRONMENT"
    )

    auth_secret_key: SecretStr = Field(alias="AUTH_SECRET_KEY")
    auth_algorithm: str = Field(alias="AUTH_ALGORITHM")
    auth_access_token_expire_minutes: int = Field(
        alias="AUTH_ACCESS_TOKEN_EXPIRE_MINUTES"
    )

    # --- Login throttling -------------------------------------------------
    # Off is for the test suite, not for a deployment.
    auth_lockout_enabled: bool = Field(default=True, alias="AUTH_LOCKOUT_ENABLED")
    # Fixed window rather than sliding: the count resets once the window
    # lapses, conceding a sustained (max_attempts - 1) per window in exchange
    # for two columns instead of a table of events.
    auth_lockout_max_attempts: int = Field(
        default=5, ge=1, alias="AUTH_LOCKOUT_MAX_ATTEMPTS"
    )
    auth_lockout_window_minutes: int = Field(
        default=15, ge=1, alias="AUTH_LOCKOUT_WINDOW_MINUTES"
    )
    # The first lock lasts this long; each further lock without a successful
    # login in between doubles it.
    auth_lockout_base_minutes: int = Field(
        default=15, ge=1, alias="AUTH_LOCKOUT_BASE_MINUTES"
    )
    # Which lock becomes permanent. At the default of four: 15 minutes, then
    # 30, then 60, then an admin has to intervene.
    auth_lockout_permanent_after_locks: int = Field(
        default=4, ge=1, alias="AUTH_LOCKOUT_PERMANENT_AFTER_LOCKS"
    )

    # The same idea keyed on the caller's address, which is what catches
    # spraying across many usernames — no single account accumulates enough
    # failures to trip a per-account counter.
    auth_ip_max_failures: int = Field(default=20, ge=1, alias="AUTH_IP_MAX_FAILURES")
    auth_ip_window_minutes: int = Field(
        default=15, ge=1, alias="AUTH_IP_WINDOW_MINUTES"
    )
    auth_ip_ban_minutes: int = Field(default=15, ge=1, alias="AUTH_IP_BAN_MINUTES")
    # Proxies in front of this process, counted from the right of
    # X-Forwarded-For. A client can send that header itself and Cloud Run
    # appends rather than replaces, so the leftmost entry is attacker-chosen.
    # 1 suits Cloud Run with no load balancer; 0 disables address-based
    # throttling, the honest setting where there is no proxy at all.
    auth_trusted_proxy_hops: int = Field(
        default=1, ge=0, alias="AUTH_TRUSTED_PROXY_HOPS"
    )

    database_url: AnyUrl = Field(alias="DATABASE_URL")
    # Off by default: echoed SQL carries bind parameters, which includes
    # password hashes on every user write.
    database_echo: bool = Field(default=False, alias="DATABASE_ECHO")

    # On by default so a local run comes up with a current schema. A deployment
    # that migrates from its pipeline turns this off: on a scale-to-zero host
    # every cold start would otherwise pay for `alembic upgrade head`, and
    # concurrent starts would race for the same migration lock.
    run_migrations_on_startup: bool = Field(
        default=True, alias="RUN_MIGRATIONS_ON_STARTUP"
    )

    # Consulted once, on a database with no admin — see services/user/bootstrap.py.
    # Both must be set for the bootstrap to run.
    bootstrap_admin_username: str | None = Field(
        default=None, alias="BOOTSTRAP_ADMIN_USERNAME"
    )
    bootstrap_admin_password: SecretStr | None = Field(
        default=None, alias="BOOTSTRAP_ADMIN_PASSWORD"
    )
    bootstrap_admin_email: str | None = Field(
        default=None, alias="BOOTSTRAP_ADMIN_EMAIL"
    )

    # --- OAuth 2.1 (MCP connectors) ----------------------------------------
    # Not derivable from a request header a caller controls, so it is
    # configuration. Required in production — see the validator below.
    public_base_url: AnyHttpUrl = Field(
        default=AnyHttpUrl("http://localhost:8000"), alias="PUBLIC_BASE_URL"
    )

    # Comma-separated hosts a DCR-registered client's redirect_uri may target;
    # rules in services/oauth/redirect_allowlist.py. No default: an unset value
    # would silently block every registration, which is a configuration error
    # rather than a valid empty policy.
    # NoDecode: pydantic-settings otherwise tries to JSON-decode any complex
    # annotation (a frozenset qualifies) before a validator sees it, so a plain
    # comma-separated string never reaches the parser below.
    oauth_allowed_redirect_hosts: Annotated[frozenset[str], NoDecode] = Field(
        alias="OAUTH_ALLOWED_REDIRECT_HOSTS"
    )

    # Whether POST /oauth/register accepts anonymous Dynamic Client
    # Registration. Closed by default: the endpoint is necessarily
    # credential-free, so an open one is an unauthenticated database write any
    # stranger can repeat. Closed, `registration_endpoint` disappears from the
    # authorization server metadata and the route 404s; clients are
    # pre-registered with `POST /oauth-clients` instead. Open it only to
    # onboard a client that speaks nothing but DCR.
    oauth_registration_enabled: bool = Field(
        default=False, alias="OAUTH_REGISTRATION_ENABLED"
    )

    # --- Browser client (clients/web/index.html) -----------------------------
    # Comma-separated origins allowed to call the authenticated routes from a
    # browser — see middleware/client_cors.py. "null" is the origin of a page
    # opened from disk (file://); safe to allowlist because none of these
    # routes take a cookie, but local use only. NoDecode: see
    # oauth_allowed_redirect_hosts above.
    client_allowed_origins: Annotated[frozenset[str], NoDecode] = Field(
        default=frozenset(), alias="CLIENT_ALLOWED_ORIGINS"
    )

    # Set to serve the browser client same-origin at GET /client, from either
    # clients/web/index.html or a built clients/web/dist/index.html.
    # Same-origin means CLIENT_ALLOWED_ORIGINS need not name it.
    client_html_path: str | None = Field(default=None, alias="CLIENT_HTML_PATH")

    # --- Multi-factor authentication ---------------------------------------
    # MFA is per-account and opt-in, so there is deliberately no global on/off
    # switch: what is configurable is the shape of the challenge, not whether
    # it happens.

    # How long the token returned by the first step stays redeemable.
    mfa_challenge_ttl_minutes: int = Field(
        default=5, ge=1, alias="MFA_CHALLENGE_TTL_MINUTES"
    )
    # Thirty-second steps either side of now a TOTP code is accepted for. 1
    # tolerates about ninety seconds of clock skew; 0 demands perfectly
    # synchronised clocks and will generate support requests.
    mfa_totp_drift_steps: int = Field(default=1, ge=0, alias="MFA_TOTP_DRIFT_STEPS")
    mfa_backup_code_count: int = Field(default=10, ge=1, alias="MFA_BACKUP_CODE_COUNT")
    # The issuer an authenticator app shows beside the account name.
    mfa_issuer: str = Field(default="resume-api", alias="MFA_ISSUER")

    # Fernet keys for the TOTP secrets at rest, newest first: every key listed
    # can decrypt, the first one encrypts. Rotating is prepending a new key and
    # leaving the old one until the lazy re-seal in TotpMethod.verify has
    # worked through the enrolled accounts. Deliver it through SECRETS_DIR in
    # production, not an environment variable. NoDecode: see
    # oauth_allowed_redirect_hosts above.
    mfa_encryption_keys: Annotated[list[SecretStr], NoDecode] = Field(
        default_factory=list, alias="MFA_ENCRYPTION_KEYS"
    )

    # The docs are read, not acted on, so this is a browsing session rather
    # than a credential lifetime.
    docs_session_minutes: int = Field(default=60, ge=1, alias="DOCS_SESSION_MINUTES")

    @field_validator("oauth_allowed_redirect_hosts", mode="before")
    @classmethod
    def _parse_oauth_allowed_redirect_hosts(cls, value):
        return RedirectAllowlist.parse(value).hosts if isinstance(value, str) else value

    @field_validator("client_allowed_origins", mode="before")
    @classmethod
    def _parse_client_allowed_origins(cls, value):
        if not isinstance(value, str):
            return value
        return frozenset(
            origin.strip() for origin in value.split(",") if origin.strip()
        )

    @field_validator("mfa_encryption_keys", mode="before")
    @classmethod
    def _parse_mfa_encryption_keys(cls, value):
        if not isinstance(value, str):
            return value
        return [key.strip() for key in value.split(",") if key.strip()]

    @field_validator("mfa_encryption_keys", mode="after")
    @classmethod
    def _validate_mfa_encryption_keys(cls, value):
        """Reject key material Fernet cannot use, at settings load.

        A malformed key is otherwise a 500 on the first enrolment and a lockout
        on every login after it.
        """
        for key in value:
            try:
                Fernet(key.get_secret_value().encode())
            except (ValueError, TypeError) as error:
                raise ValueError(
                    "MFA_ENCRYPTION_KEYS entries must be url-safe base64 "
                    "encoded 32-byte keys. Generate one with: python -c "
                    '"from cryptography.fernet import Fernet; '
                    'print(Fernet.generate_key().decode())"'
                ) from error
        return value

    @field_validator("environment", mode="before")
    @classmethod
    def _normalise_environment(cls, value):
        """Accept PRODUCTION and Production as well as production.

        An unrecognised value is still rejected — silently falling back to
        development would turn a typo into an open production deployment.
        """
        return value.strip().lower() if isinstance(value, str) else value

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def public_base_url_str(self) -> str:
        """`public_base_url`, with the trailing slash `AnyHttpUrl` adds stripped.

        Every OAuth URL in this service is built from this form, so the strip
        happens once here rather than at each call site.
        """
        return str(self.public_base_url).rstrip("/")

    @property
    def oauth_resource_url(self) -> str:
        """The exact string an OAuth access token's `aud` claim must equal.

        The MCP app is mounted at `/resume` but only knows its own `/mcp`
        suffix, so the full resource URL is assembled here rather than derived
        from the mount — see the RemoteAuthProvider construction in main.py.
        """
        return f"{self.public_base_url_str}/resume/mcp"

    @model_validator(mode="after")
    def _require_public_base_url_in_production(self) -> ConfigServiceModel:
        if self.is_production and "public_base_url" not in self.model_fields_set:
            raise ValueError(
                "PUBLIC_BASE_URL is required in production: the authorization "
                "server and resource server cannot derive their own URL from "
                "a request a caller controls."
            )
        return self

    @model_validator(mode="after")
    def _require_mfa_encryption_key_in_production(self) -> ConfigServiceModel:
        """Plaintext TOTP secrets are a development affordance, not a
        deployment option."""
        if self.is_production and not self.mfa_encryption_keys:
            raise ValueError(
                "MFA_ENCRYPTION_KEYS is required in production: TOTP secrets "
                "are symmetric, and storing them in the clear makes a database "
                "read sufficient to mint second factors."
            )
        return self


class ConfigService(ServiceProviderInterface):
    _cached: ClassVar[ConfigServiceModel | None] = None

    def __init__(self, settings: ConfigServiceModel | None):
        if settings is None:
            raise UnexpectedNoSettings(
                "Settings are not initialized. This should not happen."
            )
        self.settings = settings

    @classmethod
    def get_with_deps(cls) -> ConfigService:
        """Settings are read once and reused for the life of the process.

        This runs as a dependency on every request, so the cache saves re-
        reading .env from disk each time; changing configuration needs a
        restart. Two first requests can race on the check-then-assign, which is
        harmless — both build the same values from the same environment.
        """
        if cls._cached is None:
            cls._cached = ConfigServiceModel()
        return ConfigService(cls._cached)

    @classmethod
    def get_without_deps(cls) -> ConfigService:
        """For callers outside the request cycle: startup, the CLI, MCP."""
        return cls.get_with_deps()

    @classmethod
    def reset(cls) -> None:
        """Drop the cached settings so the next call rebuilds them."""
        cls._cached = None
