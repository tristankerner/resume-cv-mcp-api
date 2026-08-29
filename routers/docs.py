"""Swagger UI, ReDoc, and the schema they render.

FastAPI mounts these three itself, without a credential. In production that is
a public index of every route the service has, its payload shapes and its
authentication scheme, so they are re-declared here behind a login instead —
`main` disables the built-ins, which is what frees the paths.

All three, not just the two pages. The UIs are only renderers for
`/openapi.json`; guarding them while leaving the schema open would guard
nothing. Outside production `require_docs_access` waves everyone through, so
what a developer browses locally is the same surface that is deployed, minus
the prompt.
"""

from typing import Annotated, ClassVar

from fastapi import APIRouter, Depends, Request
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse, JSONResponse

from services.auth.auth_service import AuthService


class DocsRouter:
    DocsAccess = Annotated[None, Depends(AuthService.require_docs_access)]

    OPENAPI_URL: ClassVar[str] = "/openapi.json"
    DOCS_URL: ClassVar[str] = "/docs"
    REDOC_URL: ClassVar[str] = "/redoc"
    OAUTH2_REDIRECT_URL: ClassVar[str] = "/docs/oauth2-redirect"

    def __init__(self) -> None:
        # The documentation is not itself part of the documentation.
        self.router = APIRouter(include_in_schema=False)
        self._register()

    def _register(self) -> None:
        self.router.get(self.OPENAPI_URL)(self.openapi_schema)
        self.router.get(self.DOCS_URL)(self.swagger_ui)
        self.router.get(self.OAUTH2_REDIRECT_URL)(self.swagger_ui_oauth2_redirect)
        self.router.get(self.REDOC_URL)(self.redoc)

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


router = DocsRouter().router
