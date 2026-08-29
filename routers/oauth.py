"""OAuth 2.1 authorization server — RFC 7591 registration, the browser-facing
login+consent flow, RFC 6749 token issuance, and RFC 7009 revocation.

Kept thin per the project's convention: everything with a decision to make
lives in services/oauth/oauth_service.py; a route here does one call and
turns whatever it raises into the right response shape.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService
from services.oauth.authorize_page import AuthorizePageRenderer
from services.oauth.dtos import (
    AuthorizationServerMetadata,
    ClientRegistrationRequest,
    ClientRegistrationResponse,
    TokenResponse,
)
from services.oauth.exceptions import (
    AuthorizeFatalError,
    AuthorizeLoginFailed,
    AuthorizeRedirectError,
    OAuthError,
)
from services.oauth.oauth_service import OAuthService

log = logging.getLogger("uvicorn")


class OAuthRouter:
    DbSession = Annotated[AsyncSession, Depends(DatabaseService.get_async_db_session)]
    Settings = Annotated[ConfigService, Depends(ConfigService.get_with_deps)]

    def __init__(self) -> None:
        self.router = APIRouter(tags=["oauth"])
        self._register()

    def _register(self) -> None:
        self.router.get(
            "/.well-known/oauth-authorization-server",
            # `registration_endpoint` is None when registration is closed,
            # and RFC 8414 wants it absent rather than null — a null there
            # is a malformed metadata document, not a signal.
            response_model_exclude_none=True,
        )(self.authorization_server_metadata)
        self.router.post("/oauth/register", response_model=None)(self.register_client)
        self.router.get(
            "/oauth/authorize", include_in_schema=False, response_model=None
        )(self.authorize_page)
        self.router.post(
            "/oauth/authorize", include_in_schema=False, response_model=None
        )(self.authorize_submit)
        self.router.post("/oauth/token", response_model=None)(self.token_endpoint)
        self.router.post("/oauth/revoke", response_model=None)(self.revoke_endpoint)

    @staticmethod
    def _error_response(exc: OAuthError) -> JSONResponse:
        """RFC 6749 §5.2 / RFC 7591 §3.2.2 flat shape, not the app's usual
        `{"detail": ...}`."""
        body = {"error": exc.error}
        if exc.description:
            body["error_description"] = exc.description
        return JSONResponse(status_code=exc.status_code, content=body)

    @staticmethod
    def _redirect_error(
        redirect_uri: str, exc: AuthorizeRedirectError
    ) -> RedirectResponse:
        url = OAuthService._append_query(
            redirect_uri,
            {
                "error": exc.error,
                "error_description": exc.description,
                "state": exc.state,
            },
        )
        return RedirectResponse(url, status_code=302)

    async def authorization_server_metadata(
        self, db: DbSession, config_service: Settings
    ) -> AuthorizationServerMetadata:
        """RFC 8414. Served at the origin root — clients derive it from the
        issuer, never from a mounted path."""
        return OAuthService(db, config_service).authorization_server_metadata()

    async def register_client(
        self,
        request: ClientRegistrationRequest,
        db: DbSession,
        config_service: Settings,
    ) -> ClientRegistrationResponse | JSONResponse:
        """Anonymous Dynamic Client Registration, off unless
        `OAUTH_REGISTRATION_ENABLED` is set.

        404 rather than 403 when closed, deliberately: the metadata document
        does not advertise a `registration_endpoint` in that state, so as far
        as any conforming client is concerned this route does not exist, and
        saying "forbidden" would instead confirm it does and invite retries.
        Nothing here touches the database before the check, so a closed
        deployment has no anonymous write path at all.
        """
        if not config_service.settings.oauth_registration_enabled:
            log.warning(
                "Refused client registration: OAUTH_REGISTRATION_ENABLED is off. "
                "Pre-register with `python -m register_oauth_client`."
            )
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        try:
            return await OAuthService(db, config_service).register_client(request)
        except OAuthError as exc:
            log.warning("Rejected client registration: %s", exc)
            return self._error_response(exc)

    async def authorize_page(
        self,
        db: DbSession,
        config_service: Settings,
        client_id: str,
        redirect_uri: str,
        response_type: str = "code",
        code_challenge: str | None = None,
        code_challenge_method: str | None = None,
        scope: str | None = None,
        state: str | None = None,
        resource: str | None = None,
    ) -> HTMLResponse | RedirectResponse:
        oauth = OAuthService(db, config_service)
        try:
            client, requested = await oauth.prepare_authorize(
                client_id=client_id,
                redirect_uri=redirect_uri,
                response_type=response_type,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                scope=scope,
                state=state,
            )
        except AuthorizeFatalError as exc:
            log.warning("Rejected /oauth/authorize: %s", exc.detail)
            return HTMLResponse(
                AuthorizePageRenderer.render_error_page(exc.detail), status_code=400
            )
        except AuthorizeRedirectError as exc:
            return self._redirect_error(redirect_uri, exc)

        html = AuthorizePageRenderer.render_authorize_form(
            client=client,
            scopes=requested,
            client_id=client_id,
            redirect_uri=redirect_uri,
            response_type=response_type,
            code_challenge=code_challenge or "",
            code_challenge_method=code_challenge_method or "",
            scope=scope or "",
            state=state,
            resource=resource,
        )
        return HTMLResponse(html)

    async def authorize_submit(
        self,
        db: DbSession,
        config_service: Settings,
        request: Request,
        # Every field defaults to "" rather than being required: Starlette's
        # form parser treats a submitted-but-empty value as absent, which
        # would otherwise make FastAPI reject it with its own generic 422
        # before this handler ever runs — where it needs to become a proper
        # `invalid_request` redirect instead (a missing code_challenge,
        # chiefly). OAuthService validates emptiness itself; nothing here is
        # truly optional.
        client_id: Annotated[str, Form()] = "",
        redirect_uri: Annotated[str, Form()] = "",
        response_type: Annotated[str, Form()] = "",
        code_challenge: Annotated[str, Form()] = "",
        code_challenge_method: Annotated[str, Form()] = "",
        scope: Annotated[str, Form()] = "",
        state: Annotated[str, Form()] = "",
        resource: Annotated[str, Form()] = "",
        username: Annotated[str, Form()] = "",
        password: Annotated[str, Form()] = "",
        decision: Annotated[str, Form()] = "deny",
    ) -> HTMLResponse | RedirectResponse:
        oauth = OAuthService(db, config_service)
        try:
            url = await oauth.complete_authorize(
                request,
                client_id=client_id,
                redirect_uri=redirect_uri,
                response_type=response_type,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                scope=scope,
                state=state or None,
                resource=resource or None,
                username=username,
                password=password,
                approved=(decision == "approve"),
            )
        except AuthorizeFatalError as exc:
            log.warning("Rejected /oauth/authorize: %s", exc.detail)
            return HTMLResponse(
                AuthorizePageRenderer.render_error_page(exc.detail), status_code=400
            )
        except AuthorizeRedirectError as exc:
            return self._redirect_error(redirect_uri, exc)
        except AuthorizeLoginFailed as exc:
            # client_id/redirect_uri were just re-validated inside
            # complete_authorize, so re-running the same check to rebuild the
            # form cannot itself raise AuthorizeFatalError/RedirectError here.
            client, requested = await oauth.prepare_authorize(
                client_id=client_id,
                redirect_uri=redirect_uri,
                response_type=response_type,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                scope=scope,
                state=state or None,
            )
            html = AuthorizePageRenderer.render_authorize_form(
                client=client,
                scopes=requested,
                client_id=client_id,
                redirect_uri=redirect_uri,
                response_type=response_type,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                scope=scope,
                state=state or None,
                resource=resource or None,
                error=exc.detail,
            )
            return HTMLResponse(html, status_code=401)

        return RedirectResponse(url, status_code=303)

    async def token_endpoint(
        self,
        db: DbSession,
        config_service: Settings,
        grant_type: Annotated[str, Form()],
        client_id: Annotated[str | None, Form()] = None,
        client_secret: Annotated[str | None, Form()] = None,
        code: Annotated[str | None, Form()] = None,
        redirect_uri: Annotated[str | None, Form()] = None,
        code_verifier: Annotated[str | None, Form()] = None,
        refresh_token: Annotated[str | None, Form()] = None,
    ) -> TokenResponse | JSONResponse:
        try:
            return await OAuthService(db, config_service).exchange_token(
                grant_type=grant_type,
                client_id=client_id,
                client_secret=client_secret,
                code=code,
                redirect_uri=redirect_uri,
                code_verifier=code_verifier,
                refresh_token=refresh_token,
            )
        except OAuthError as exc:
            return self._error_response(exc)

    async def revoke_endpoint(
        self,
        db: DbSession,
        config_service: Settings,
        token: Annotated[str, Form()],
        client_id: Annotated[str | None, Form()] = None,
        client_secret: Annotated[str | None, Form()] = None,
        token_type_hint: Annotated[str | None, Form()] = None,
    ) -> Response | JSONResponse:
        try:
            await OAuthService(db, config_service).revoke_token(
                token=token, client_id=client_id, client_secret=client_secret
            )
        except OAuthError as exc:
            return self._error_response(exc)
        # RFC 7009 §2.2: 200 regardless of whether the token existed, was
        # already revoked, or belonged to someone else — that distinction is
        # not the caller's to learn.
        return Response(status_code=200)


router = OAuthRouter().router
