"""Swagger UI, ReDoc, and the schema they render — plus the login in front
of them in production.

FastAPI mounts the first three itself, without a credential. In production
that is a public index of every route the service has, its payload shapes
and its authentication scheme, so they are re-declared here behind a login
instead — `main` disables the built-ins, which is what frees the paths.

All three, not just the two pages: the UIs are only renderers for
`/openapi.json`, so guarding them while leaving the schema open would guard
nothing. Outside production `DocsAccessGuard.check` waves everyone through.

The login is an HTML form, not HTTP Basic: a password, then — for an
MFA-enrolled account — a code, then a signed `HttpOnly` cookie. This is the
one surface in the service that takes a cookie at all.
"""

from typing import Annotated, ClassVar

from fastapi import APIRouter, Depends, Form, Request
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.user import User
from services.auth.auth_service import AuthService
from services.auth.docs_login_page import DocsLoginPageRenderer
from services.auth.docs_session import DocsAccessGuard, DocsSessionToken
from services.auth.mfa.challenge import MfaChallengeContext, MfaChallengeToken
from services.auth.mfa.verifier import MfaVerifier
from services.config.config_service import ConfigService, ConfigServiceModel
from services.database.database_service import DatabaseService


class DocsRouter:
    DocsAccess = Annotated[None, Depends(DocsAccessGuard.check)]
    DbSession = Annotated[AsyncSession, Depends(DatabaseService.get_async_db_session)]
    Settings = Annotated[ConfigService, Depends(ConfigService.get_with_deps)]

    OPENAPI_URL: ClassVar[str] = "/openapi.json"
    DOCS_URL: ClassVar[str] = "/docs"
    REDOC_URL: ClassVar[str] = "/redoc"
    OAUTH2_REDIRECT_URL: ClassVar[str] = "/docs/oauth2-redirect"
    LOGIN_URL: ClassVar[str] = "/docs/login"

    # An open-redirect guard: `next` arrives as attacker-controlled query or
    # form input, and the only acceptable destinations are the docs paths this
    # router serves.
    NEXT_ALLOWLIST: ClassVar[frozenset[str]] = frozenset(
        {OPENAPI_URL, DOCS_URL, REDOC_URL, OAUTH2_REDIRECT_URL}
    )

    def __init__(self) -> None:
        # The documentation is not itself part of the documentation.
        self.router = APIRouter(include_in_schema=False)
        self._register()

    def _register(self) -> None:
        self.router.get(self.OPENAPI_URL)(self.openapi_schema)
        self.router.get(self.DOCS_URL)(self.swagger_ui)
        self.router.get(self.OAUTH2_REDIRECT_URL)(self.swagger_ui_oauth2_redirect)
        self.router.get(self.REDOC_URL)(self.redoc)
        self.router.get(self.LOGIN_URL, response_model=None)(self.login_page)
        self.router.post(self.LOGIN_URL, response_model=None)(self.login_submit)
        self.router.post("/docs/logout")(self.logout)

    async def openapi_schema(self, request: Request, _: DocsAccess) -> JSONResponse:
        """The generated schema, built and cached by the application object."""
        return JSONResponse(request.app.openapi())

    async def swagger_ui(self, request: Request, _: DocsAccess) -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=self.OPENAPI_URL,
            title=f"{request.app.title} - Swagger UI",
            oauth2_redirect_url=self.OAUTH2_REDIRECT_URL,
            # Read off the app rather than restated, so configuring the UI
            # stays a one-line change where the app is created.
            swagger_ui_parameters=request.app.swagger_ui_parameters,
        )

    async def swagger_ui_oauth2_redirect(self, _: DocsAccess) -> HTMLResponse:
        """The callback page Swagger UI hands to an OAuth2 provider.

        Static, and unused by the password flow `/token` implements, but it
        is part of the stock docs setup and Swagger UI is told above that it
        exists.
        """
        return get_swagger_ui_oauth2_redirect_html()

    async def redoc(self, request: Request, _: DocsAccess) -> HTMLResponse:
        return get_redoc_html(
            openapi_url=self.OPENAPI_URL, title=f"{request.app.title} - ReDoc"
        )

    async def login_page(
        self,
        request: Request,
        db: DbSession,
        config_service: Settings,
        next: str = DOCS_URL,
    ) -> HTMLResponse | RedirectResponse:
        next_path = self._validate_next(next)
        token = request.cookies.get(DocsSessionToken.COOKIE_NAME)
        if token:
            user = await DocsSessionToken.user_from_cookie(
                db, config_service.settings, token
            )
            if user is not None:
                return RedirectResponse(next_path, status_code=303)
        return HTMLResponse(
            DocsLoginPageRenderer.render_login_form(next_path=next_path)
        )

    async def login_submit(
        self,
        request: Request,
        db: DbSession,
        config_service: Settings,
        # Every field defaults to "" for the same reason authorize_submit in
        # routers/oauth.py does: a missing value should re-render this form
        # with an error, not 422.
        username: Annotated[str, Form()] = "",
        password: Annotated[str, Form()] = "",
        next: Annotated[str, Form()] = DOCS_URL,
        mfa_token: Annotated[str, Form()] = "",
        code: Annotated[str, Form()] = "",
    ) -> HTMLResponse | RedirectResponse:
        next_path = self._validate_next(next)
        settings = config_service.settings
        auth_service = AuthService(db, None, config_service, request)
        verifier = MfaVerifier(db, settings)

        if mfa_token:
            user = await verifier.user_from_challenge(
                mfa_token, MfaChallengeContext.DOCS
            )
            if user is None:
                html = DocsLoginPageRenderer.render_login_form(
                    next_path=next_path,
                    error="The login attempt expired. Start again.",
                )
                return HTMLResponse(html, status_code=401)
            # The same check /token/mfa and /oauth/authorize make before
            # accepting a code — see AuthService.raise_if_locked.
            auth_service.raise_if_locked(user)
            if not await verifier.verify(user, code):
                await auth_service.register_mfa_failure(user)
                html = DocsLoginPageRenderer.render_mfa_form(
                    next_path=next_path,
                    mfa_token=mfa_token,
                    error="That code is not valid.",
                )
                return HTMLResponse(html, status_code=401)
            await auth_service.clear_login_failures(user)
            return self._issue_cookie(user, next_path, settings)

        # AuthErrors.account_locked(_permanently) is deliberately not caught:
        # letting it out as a 429/401 keeps the docs login from being a way
        # around the throttle /token uses.
        user = await auth_service.authenticate_user(username, password)
        if not user:
            html = DocsLoginPageRenderer.render_login_form(
                next_path=next_path, error="Incorrect username or password."
            )
            return HTMLResponse(html, status_code=401)

        if await verifier.is_enrolled(user):
            token, _expires_in = MfaChallengeToken.mint(
                settings, user, MfaChallengeContext.DOCS
            )
            html = DocsLoginPageRenderer.render_mfa_form(
                next_path=next_path, mfa_token=token
            )
            return HTMLResponse(html)

        return self._issue_cookie(user, next_path, settings)

    async def logout(self) -> RedirectResponse:
        response = RedirectResponse(self.LOGIN_URL, status_code=303)
        response.delete_cookie(DocsSessionToken.COOKIE_NAME, path="/")
        return response

    @classmethod
    def _validate_next(cls, next_path: str) -> str:
        return next_path if next_path in cls.NEXT_ALLOWLIST else cls.DOCS_URL

    @staticmethod
    def _issue_cookie(
        user: User, next_path: str, settings: ConfigServiceModel
    ) -> RedirectResponse:
        token, expires_in = DocsSessionToken.mint(settings, user)
        response = RedirectResponse(next_path, status_code=303)
        # Path=/ rather than /docs: /openapi.json and /redoc are outside a
        # /docs prefix. Secure only in production — a local run over plain HTTP
        # would never see the cookie come back.
        response.set_cookie(
            DocsSessionToken.COOKIE_NAME,
            token,
            max_age=expires_in,
            httponly=True,
            samesite="lax",
            path="/",
            secure=settings.is_production,
        )
        return response
