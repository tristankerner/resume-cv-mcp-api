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

    Only used to decide what to lock down; nothing branches on it to change
    behaviour that a developer then cannot reproduce locally. Development is
    the default so an unset value is the permissive-to-run, safe-to-forget one
    — but note that means a production deployment has to say so explicitly.
    """

    DEVELOPMENT = auto()
    PRODUCTION = auto()


class ConfigServiceModel(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Opt-in, so local runs are unaffected. A container sets
        # SECRETS_DIR=/run/secrets and any setting below can then be supplied
        # as a Docker secret file instead of an environment variable — env vars
        # are readable via `docker inspect` and leak into child processes.
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
    # Off switches the whole mechanism, account and address alike, and is
    # meant for the test suite rather than for a deployment: a lockout that is
    # disabled locally is a lockout nobody notices is broken.
    auth_lockout_enabled: bool = Field(default=True, alias="AUTH_LOCKOUT_ENABLED")
    # Failures inside the window that trip a lock, and how far back the window
    # reaches. The window is fixed rather than sliding: the count resets once
    # it lapses, which concedes a sustained (max_attempts - 1) per window to a
    # patient attacker and costs two columns instead of a table of events.
    auth_lockout_max_attempts: int = Field(
        default=5, ge=1, alias="AUTH_LOCKOUT_MAX_ATTEMPTS"
    )
    auth_lockout_window_minutes: int = Field(
        default=15, ge=1, alias="AUTH_LOCKOUT_WINDOW_MINUTES"
    )
    # The first lock lasts this long; each further lock without a successful
    # login in between doubles it. Escalating is what removes the need for a
    # second "N locks within M minutes" window — the curve already encodes it.
    auth_lockout_base_minutes: int = Field(
        default=15, ge=1, alias="AUTH_LOCKOUT_BASE_MINUTES"
    )
    # Which lock becomes permanent. At the default of four: 15 minutes, then
    # 30, then 60, then an admin has to intervene. A successful login resets
    # the count, so this is only reached by an account nobody is logging into.
    auth_lockout_permanent_after_locks: int = Field(
        default=4, ge=1, alias="AUTH_LOCKOUT_PERMANENT_AFTER_LOCKS"
    )

    # The same idea keyed on the caller's address, which is what catches
    # spraying across many usernames — a per-account counter never sees it,
    # because no single account accumulates enough failures to trip.
    auth_ip_max_failures: int = Field(default=20, ge=1, alias="AUTH_IP_MAX_FAILURES")
    auth_ip_window_minutes: int = Field(
        default=15, ge=1, alias="AUTH_IP_WINDOW_MINUTES"
    )
    auth_ip_ban_minutes: int = Field(default=15, ge=1, alias="AUTH_IP_BAN_MINUTES")
    # How many proxies sit in front of this process, counted from the right of
    # X-Forwarded-For. A client can send that header itself and Cloud Run
    # appends rather than replaces, so the leftmost entry is attacker-chosen;
    # only a fixed number of hops from the right is trustworthy. 1 is correct
    # for Cloud Run with no load balancer in front. 0 disables address-based
    # throttling, which is the honest setting for a deployment with no proxy,
    # where the header cannot be trusted at all.
    auth_trusted_proxy_hops: int = Field(
        default=1, ge=0, alias="AUTH_TRUSTED_PROXY_HOPS"
    )

    database_url: AnyUrl = Field(alias="DATABASE_URL")
    # Off by default: echoed SQL carries bind parameters, which includes
    # password hashes on every user write.
    database_echo: bool = Field(default=False, alias="DATABASE_ECHO")

    # On by default, so a local run or a compose file comes up with a current
    # schema and nothing extra to remember. A deployment that migrates from its
    # pipeline turns this off: on a scale-to-zero host every cold start would
    # otherwise pay for `alembic upgrade head` before serving its first request,
    # and concurrent starts would race for the same migration lock.
    #
    # Turning it off makes the pipeline responsible for the schema. That is the
    # intended trade: if the migration step fails, startup then fails too — the
    # bootstrap below is the first thing to touch a table — rather than serving
    # against a schema the code does not expect.
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
    # The authorization server's issuer URL and the resource server's resource
    # URL. Neither is derivable from a request header a caller controls, so
    # both are configuration. Optional locally, where a developer talks to the
    # service directly; required in production, where getting it wrong rejects
    # every OAuth token with no useful error, so it fails loudly at startup
    # instead.
    public_base_url: AnyHttpUrl = Field(
        default=AnyHttpUrl("http://localhost:8000"), alias="PUBLIC_BASE_URL"
    )

    # Comma-separated hosts a DCR-registered client's redirect_uri may target.
    # No default: an empty or unset value would silently block every
    # registration, which is a configuration error, not a valid empty policy.
    # Full parsing and validation rules in services/oauth/redirect_allowlist.py.
    # NoDecode: pydantic-settings otherwise tries to JSON-decode any complex
    # annotation (a frozenset qualifies) before a validator ever sees it, so
    # a plain comma-separated string would fail to parse before reaching
    # `_parse_oauth_allowed_redirect_hosts` below.
    oauth_allowed_redirect_hosts: Annotated[frozenset[str], NoDecode] = Field(
        alias="OAUTH_ALLOWED_REDIRECT_HOSTS"
    )

    # Whether POST /oauth/register accepts anonymous Dynamic Client
    # Registration. Closed by default, because the endpoint is necessarily
    # credential-free and therefore an unauthenticated database write that
    # anyone who finds it can repeat: the redirect allowlist bounds *where an
    # authorization code may be delivered*, not how many client rows a
    # stranger can create. Closed, `registration_endpoint` disappears from the
    # authorization server metadata (RFC 8414 makes it OPTIONAL) and the route
    # 404s, so there is no anonymous write path at all rather than a bounded
    # one. Clients are pre-registered instead — `python -m
    # register_oauth_client` — which both Claude Code (--client-id) and
    # claude.ai (Advanced settings) accept.
    #
    # Turn it on only to onboard a client that speaks nothing but DCR, and
    # turn it off again afterwards.
    oauth_registration_enabled: bool = Field(
        default=False, alias="OAUTH_REGISTRATION_ENABLED"
    )

    # --- Browser client (clients/web/index.html) -----------------------------
    # Origins allowed to call the authenticated routes (/token, /documents,
    # /api-keys, /users/*) from a browser — see middleware/client_cors.py.
    # Comma-separated, empty by default, which is exactly today's behaviour:
    # no origin is allowlisted, so the middleware is a no-op everywhere.
    # "null" is the origin of a page opened directly from disk (file://); safe
    # to allowlist because none of these routes take a cookie — the one
    # cookie this service issues (docs_session, see services/auth/docs_session.py)
    # is HttpOnly and answers only the four documentation routes, which this
    # setting does not gate at all — so a hostile page granted "null" still
    # has no way to obtain a token that lives in the client origin's
    # localStorage. Recommended for local use only, not production.
    # NoDecode: see oauth_allowed_redirect_hosts above for why a plain
    # comma-separated string needs this to reach the validator unparsed.
    client_allowed_origins: Annotated[frozenset[str], NoDecode] = Field(
        default=frozenset(), alias="CLIENT_ALLOWED_ORIGINS"
    )

    # Unset by default, so the API depends on no build artifact and nothing
    # changes for a deployment that does not want this. Set to serve the
    # client same-origin instead — either clients/web/index.html directly or a
    # built clients/web/dist/index.html — at GET /client. Same-origin means
    # CLIENT_ALLOWED_ORIGINS does not even need to name it.
    client_html_path: str | None = Field(default=None, alias="CLIENT_HTML_PATH")

    # --- Multi-factor authentication ---------------------------------------
    # MFA is per-account and opt-in, so there is deliberately no global
    # on/off switch: a setting that silently stops demanding a second factor
    # from accounts that enrolled one is a footgun, not a feature. What is
    # configurable is the shape of the challenge, not whether it happens.

    # How long the token returned by the first step stays redeemable. Long
    # enough to find a phone, short enough that one left in a shell history
    # is worthless.
    mfa_challenge_ttl_minutes: int = Field(
        default=5, ge=1, alias="MFA_CHALLENGE_TTL_MINUTES"
    )
    # Thirty-second steps either side of now that a TOTP code is accepted
    # for. 1 tolerates roughly a minute and a half of clock skew between a
    # phone and this server, which is the usual recommendation; 0 demands
    # perfectly synchronised clocks and will generate support requests.
    mfa_totp_drift_steps: int = Field(default=1, ge=0, alias="MFA_TOTP_DRIFT_STEPS")
    mfa_backup_code_count: int = Field(default=10, ge=1, alias="MFA_BACKUP_CODE_COUNT")
    # The issuer an authenticator app shows beside the account name. Purely
    # cosmetic, and worth setting when one person runs more than one of these.
    mfa_issuer: str = Field(default="resume-api", alias="MFA_ISSUER")

    # Fernet keys for the TOTP secrets at rest, newest first: every key
    # listed can decrypt, the first one encrypts. Rotating is therefore
    # prepending a new key and leaving the old one until the lazy re-seal in
    # TotpMethod.verify has worked through the enrolled accounts.
    #
    # Deliver this through SECRETS_DIR in production, not an environment
    # variable — `docker inspect` reads env vars, and this key is the whole
    # of what stands between a database dump and everyone's second factor.
    #
    # NoDecode for the same reason the two frozensets above carry it:
    # pydantic-settings tries to JSON-decode any complex annotation before a
    # validator sees it, so a plain comma-separated string never reaches
    # `_parse_mfa_encryption_keys`.
    mfa_encryption_keys: Annotated[list[SecretStr], NoDecode] = Field(
        default_factory=list, alias="MFA_ENCRYPTION_KEYS"
    )

    # How long a documentation login lasts. The docs are read, not acted on,
    # so this is a browsing session rather than a credential lifetime.
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

        A malformed key is otherwise a 500 on the first enrolment and a
        lockout on every login after it. Fernet wants exactly 32 bytes,
        url-safe base64 encoded; anything else fails here, where the message
        can say so.
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

        Deployment platforms are not consistent about case, and the cost of
        being strict here is a service that refuses to start. An unrecognised
        value is still rejected — silently falling back to development would
        turn a typo into an open production deployment.
        """
        return value.strip().lower() if isinstance(value, str) else value

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def public_base_url_str(self) -> str:
        """`public_base_url`, with any trailing slash pydantic added stripped.

        `AnyHttpUrl` normalizes a bare host into `https://host/` — this is
        the form every OAuth URL in this service is built from, so the strip
        happens once, here, rather than at each call site.
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
        deployment option. Locally there is nothing worth encrypting and a
        required key would only be ceremony before `uv run pytest`; in
        production the absence of one is a configuration mistake nobody
        notices until a database leaks."""
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

        This runs as a dependency on every request; without the cache each one
        re-read and re-validated the .env file from disk. The consequence is
        that changing configuration now needs a restart.

        The check-then-assign is not atomic, and FastAPI runs sync dependencies
        in a thread pool, so two requests can race here on the very first call.
        That is harmless: both build the same values from the same environment,
        and whichever assignment lands last is equivalent to the other.
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
