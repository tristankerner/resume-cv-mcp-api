"""The pure-ASGI wrapper, preflight detection, and response-header stamping
shared by `PublicCorsMiddleware` and `ClientCorsMiddleware`.

Pure ASGI rather than `BaseHTTPMiddleware`, in both subclasses, because the
latter buffers response bodies and the MCP app mounted at `/resume` streams.

The two differ in path scope, in whether the origin is echoed or wildcarded,
and in the preflight headers — see each subclass's own docstring for why. This
base class does not decide any of that; it only avoids restating the ASGI
plumbing twice.
"""

from abc import ABC, abstractmethod

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class CorsMiddleware(ABC):
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @abstractmethod
    def _applies_to(self, scope: Scope) -> bool:
        """Whether this middleware has anything to say about this path."""

    @abstractmethod
    def _allow_origin(self, origin: str | None) -> str | None:
        """The value for `Access-Control-Allow-Origin`, or None to pass the
        request through untouched."""

    @abstractmethod
    def _preflight_headers(
        self, allowed_origin: str, request_headers: Headers
    ) -> dict[str, str]:
        """Headers for the 204 answer to a CORS preflight."""

    def _stamp_headers(self, headers: MutableHeaders, allowed_origin: str) -> None:
        """Headers for the actual response. Overridden by the subclass that
        needs `Vary` alongside the allow-origin echo."""
        headers["access-control-allow-origin"] = allowed_origin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._applies_to(scope):
            await self.app(scope, receive, send)
            return

        request_headers = Headers(scope=scope)
        allowed_origin = self._allow_origin(request_headers.get("origin"))
        if allowed_origin is None:
            await self.app(scope, receive, send)
            return

        if (
            scope["method"] == "OPTIONS"
            and "access-control-request-method" in request_headers
        ):
            response = Response(
                status_code=204,
                headers=self._preflight_headers(allowed_origin, request_headers),
            )
            await response(scope, receive, send)
            return

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                self._stamp_headers(MutableHeaders(scope=message), allowed_origin)
            await send(message)

        await self.app(scope, receive, send_with_cors)
