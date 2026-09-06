import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import ClassVar, cast

from alembic.config import Config
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastmcp import FastMCP
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.providers import FileSystemProvider
from fastmcp.utilities.lifespan import combine_lifespans
from pydantic import AnyHttpUrl
from sqlalchemy import select

from alembic import command
from middleware.client_cors import ClientCorsMiddleware
from middleware.public_cors import PublicCorsMiddleware
from persistence.auth_failure import AuthFailure
from persistence.mfa_credential import MfaCredential
from persistence.oauth_authorization_code import OAuthAuthorizationCode
from persistence.oauth_refresh_token import OAuthRefreshToken
from routers.api_keys import ApiKeysRouter
from routers.auth import AuthRouter
from routers.docs import DocsRouter
from routers.documents import DocumentsRouter
from routers.mfa import MfaRouter
from routers.oauth import OAuthRouter
from routers.oauth_clients import OAuthClientsRouter
from routers.users import UsersRouter
from services.auth.mcp_verifier import McpTokenVerifier
from services.auth.mfa.secret_box import MfaSecretBox
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService
from services.oauth.scopes import OAuthScopes
from services.user.bootstrap import AdminBootstrapper, BootstrapOutcome


class StartupTasks:
    """Migrations, admin bootstrap, and the two startup sweeps.

    `Application._lifespan` runs them in this fixed order, once per start.
    Takes a `ConfigService` rather than reading one of its own so a caller
    controls which settings snapshot it acts on — that is what lets a test
    change the environment and immediately observe it here.
    """

    LOG: ClassVar[logging.Logger] = logging.getLogger("uvicorn")

    def __init__(self, config_service: ConfigService):
        self.config_service = config_service

    async def run_migrations(self) -> None:
        alembic_cfg = Config("alembic.ini")
        await asyncio.to_thread(command.upgrade, alembic_cfg, "head")

    async def bootstrap_admin_user(self) -> None:
        """Claim a fresh database using the configured credentials, if any.

        Lets a container come up ready to use on first start. A BootstrapError
        is left to propagate: a deployment configured with an admin it cannot
        create should fail visibly rather than serve with no way in.
        """
        settings = self.config_service.settings
        password = settings.bootstrap_admin_password

        async with DatabaseService.session() as db:
            outcome, username = await AdminBootstrapper(db).ensure_admin(
                settings.bootstrap_admin_username,
                password.get_secret_value() if password else None,
                settings.bootstrap_admin_email,
            )

        if outcome is BootstrapOutcome.CREATED:
            self.LOG.info("Created bootstrap admin %r.", username)
        elif outcome is BootstrapOutcome.NOT_CONFIGURED:
            self.LOG.warning(
                "No admin user exists. Public documents will be served, but nothing "
                "can be written until one is created: set BOOTSTRAP_ADMIN_USERNAME "
                "and BOOTSTRAP_ADMIN_PASSWORD and restart, or run "
                "`python -m admin_cli bootstrap-admin`."
            )

    async def verify_mfa_key(self) -> None:
        """Open one sealed TOTP secret, to prove the configured key is the one
        this database was written with.

        A wrong key is otherwise silent until somebody tries to log in, at
        which point every enrolled account is locked out at once. A database
        with no sealed rows has nothing to check, so this is silent on a fresh
        deployment and only speaks when it has evidence.
        """
        async with DatabaseService.session() as db:
            sealed = (
                (
                    await db.execute(
                        select(MfaCredential)
                        .where(MfaCredential.secret.like(f"{MfaSecretBox.PREFIX}%"))
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
        if sealed is None or sealed.secret is None:
            return

        box = MfaSecretBox.from_settings(self.config_service.settings)
        if box.open(sealed.secret) is None:
            raise RuntimeError(
                "MFA_ENCRYPTION_KEYS does not open an existing sealed TOTP "
                "secret. Every enrolled account would be locked out of "
                "password login; refusing to start rather than serve that."
            )

    async def prune_oauth_grants(self) -> None:
        """Drop authorization codes and refresh tokens past their TTL.

        Same startup-sweep placement as `prune_auth_failures`. A
        revoked-but-unexpired refresh token is kept — see
        `OAuthRefreshToken.prune` — since presenting it again is the reuse
        signal the rotation chain depends on.
        """
        async with DatabaseService.session() as db:
            codes_removed = await OAuthAuthorizationCode.prune(db)
            tokens_removed = await OAuthRefreshToken.prune(db)
        if codes_removed:
            self.LOG.info(
                "Pruned %d expired OAuth authorization code(s).", codes_removed
            )
        if tokens_removed:
            self.LOG.info("Pruned %d expired OAuth refresh token(s).", tokens_removed)

    async def prune_auth_failures(self) -> None:
        """Drop login-failure rows that no longer decide anything.

        Startup is the whole schedule: there is no scheduler in this service. A
        row is only removed once it is both outside the counting window and
        past any ban, so a sweep that never runs costs disk, not correctness.
        """
        settings = self.config_service.settings
        window = timedelta(
            minutes=max(settings.auth_ip_window_minutes, settings.auth_ip_ban_minutes)
        )
        async with DatabaseService.session() as db:
            removed = await AuthFailure.prune(db, window)
        if removed:
            self.LOG.info("Pruned %d expired login-failure record(s).", removed)


class ClientRoute:
    """Serves the browser client at GET /client, if one is configured.

    Served same-origin, so CLIENT_ALLOWED_ORIGINS does not need to name it.
    Which file is the deployment's business: clients live in their own
    repositories and one is copied into the build context before
    `COPY . /app`. .dockerignore keeps a locally built
    clients/dist/index.html out of any image on purpose — it is
    gitignored, so nothing reviews what would be served.

    Its own class rather than a method on `Application` so the resolved path
    has somewhere to live that is not a closure over `register`.
    """

    LOG: ClassVar[logging.Logger] = logging.getLogger("uvicorn")

    # No `script-src`, deliberately: the client is one self-contained file
    # whose script is inlined by its build, so locking scripts down would need
    # either `unsafe-inline` — which buys nothing — or a hash that changes with
    # every client build. Nothing is fetched from anywhere else, which is the
    # property that made a CDN allowlist unnecessary in the first place.
    # `frame-ancestors` matters because the page carries a login form.
    HEADERS: ClassVar[dict[str, str]] = {
        "Content-Security-Policy": (
            "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def register(cls, app_: FastAPI, configured_path: str | None) -> bool:
        """The path is resolved once here rather than per request: declining to
        register the route is more honest than a 404 that never changes.

        An instance is built only once there is a real file, which is what
        keeps `path` non-optional for `serve`. Returns whether it registered.
        """
        if not configured_path:
            return False

        path = Path(configured_path)
        if not path.is_file():
            cls.LOG.warning(
                "CLIENT_HTML_PATH is set to %r but that file does not exist; "
                "GET /client will not be registered.",
                configured_path,
            )
            return False

        app_.get("/client", include_in_schema=False)(cls(path).serve)
        return True

    async def serve(self) -> FileResponse:
        return FileResponse(self.path, media_type="text/html", headers=self.HEADERS)


class Application:
    """Assembles the ASGI app: MCP mount, middleware, routers, client route."""

    LOG: ClassVar[logging.Logger] = logging.getLogger("uvicorn")

    def __init__(self, config_service: ConfigService | None = None):
        self.config_service = config_service or ConfigService.get_without_deps()
        self.settings = self.config_service.settings

        auth_provider = self._build_auth_provider()
        mcp = FastMCP(
            "MCP Retrieval",
            auth=auth_provider,
            providers=[FileSystemProvider(Path(__file__).parent / "routers")],
        )
        mcp_app = mcp.http_app(path="/mcp")

        self._app = FastAPI(
            lifespan=combine_lifespans(self._lifespan, mcp_app.lifespan),
            swagger_ui_parameters={"defaultModelsExpandDepth": -1},
            # The built-in docs are served without a credential. Switching them
            # off leaves the three paths free for routers/docs.py, which serves
            # them behind a login.
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )
        self._app.mount("/resume", mcp_app)

        self._app.add_exception_handler(
            RequestValidationError, self._handle_validation_error
        )

        # The résumé page is a separate site on a separate origin, so the
        # public document routes have to declare themselves cross-origin
        # readable or a browser will fetch them and then discard the response.
        self._app.add_middleware(PublicCorsMiddleware)

        self._app.add_middleware(ClientCorsMiddleware)

        # RFC 9728 requires the protected-resource document at the origin root,
        # but http_app() registers it under the /resume mount, where a client's
        # discovery would never look. The copy left inside the sub-app is
        # harmless; this is the one that is actually reached.
        for route in auth_provider.get_well_known_routes(mcp_path="/mcp"):
            self._app.router.routes.append(route)

        # Each router class owns its own APIRouter; instantiating here rather
        # than at module import is what keeps `routers/` free of globals.
        for router in (
            DocsRouter(),
            UsersRouter(),
            AuthRouter(),
            DocumentsRouter(),
            ApiKeysRouter(),
            OAuthRouter(),
            OAuthClientsRouter(),
            MfaRouter(),
        ):
            self._app.include_router(router.router)

        ClientRoute.register(self._app, self.settings.client_html_path)

        self._app.get("/")(self.root)

    async def root(self) -> dict[str, str]:
        return {"I'm": "alive"}

    @property
    def app(self) -> FastAPI:
        return self._app

    def _build_auth_provider(self) -> RemoteAuthProvider:
        """Resource server (this endpoint) plus the authorization servers a
        client may trust to issue it a token — here, just this service's own
        AS at /oauth/*. `resource_base_url` compensates for the /resume mount:
        http_app() only knows its own "/mcp" suffix, so left unset the
        advertised resource URL would be wrong by construction.
        """
        settings = self.settings
        return RemoteAuthProvider(
            token_verifier=McpTokenVerifier(),
            # AnyHttpUrl always renders this with a trailing slash, and that is
            # the string a client adopts as the issuer identifier. RFC 8414
            # §3.3 then requires the `issuer` from
            # /.well-known/oauth-authorization-server to be identical to it, so
            # services/oauth/oauth_service.py renders its issuer the same way.
            # Change either one and change both.
            authorization_servers=[AnyHttpUrl(settings.public_base_url_str)],
            base_url=settings.public_base_url_str,
            resource_base_url=f"{settings.public_base_url_str}/resume",
            # Only what the MCP tools actually use. A client takes its scope
            # request straight from this list, and an OAuth token authenticates
            # on the REST surface too, so advertising the whole enum would let
            # a connector ask for — and get — users:admin.
            scopes_supported=sorted(str(scope) for scope in OAuthScopes.ISSUABLE),
            resource_name="resume-cv-mcp-api MCP",
        )

    @staticmethod
    async def _handle_validation_error(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        """FastAPI's 422, minus the echo of what was sent.

        The stock handler puts the offending value in an `input` key, so a
        request failing validation on some *other* field still echoes back
        whatever else it carried — and several routes here carry a password.
        `SecretStr` does not help: `input` is the raw body as it arrived,
        before any field was parsed into one.

        Nothing needs the key; the browser client reads `loc` and `msg`.
        """
        errors = [
            {key: value for key, value in error.items() if key != "input"}
            for error in cast(RequestValidationError, exc).errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": jsonable_encoder(errors)},
        )

    @asynccontextmanager
    async def _lifespan(self, app_: FastAPI) -> AsyncIterator[None]:
        self.LOG.info("Starting up...")
        # A fresh ConfigService, not self.config_service: tests reuse the same
        # Application across many runs after monkeypatching the environment,
        # and only a fresh read observes that.
        tasks = StartupTasks(ConfigService.get_without_deps())
        if tasks.config_service.settings.run_migrations_on_startup:
            self.LOG.info("run alembic upgrade head...")
            await tasks.run_migrations()
        else:
            self.LOG.info(
                "Skipping migrations: RUN_MIGRATIONS_ON_STARTUP is off, so the "
                "deployment pipeline owns the schema."
            )
        await tasks.bootstrap_admin_user()
        await tasks.verify_mfa_key()
        await tasks.prune_auth_failures()
        await tasks.prune_oauth_grants()
        yield
        self.LOG.info("Shutting down...")


application = Application()
app = application.app  # `main:app` — the entrypoint in pyproject.toml
