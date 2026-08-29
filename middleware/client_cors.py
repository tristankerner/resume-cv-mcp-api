"""Cross-origin access to the authenticated routes, for a browser-based client.

`/token`, `/documents`, `/api-keys` and `/users/me/` send no CORS headers at
all today, so a browser on any other origin — including `file://`, whose
`Origin` is the literal string `null` — will make the request and then throw
the response away. This middleware lets specific, operator-chosen origins
through; everything else is unaffected.
"""

from typing import ClassVar

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import Scope

from middleware.cors_base import CorsMiddleware
from middleware.public_cors import PUBLIC_PATH_PREFIX
from services.config.config_service import ConfigService

ALLOW_ORIGIN = "access-control-allow-origin"
VARY = "vary"


class ClientCorsMiddleware(CorsMiddleware):
    """Echo an allowlisted `Origin` back on the non-public routes.

    **Echo-and-`Vary`, not a literal `*`.** Unlike `/public/*`, these routes
    are not behind a cache that ignores `Vary` — see PublicCorsMiddleware's
    docstring for why that rule is different there — so an allowlist that
    echoes the caller's own origin is both safe and tighter than a wildcard
    would be.

    **No `Access-Control-Allow-Credentials`.** Every credential here travels
    in an `Authorization` header the client sets explicitly; there are no
    cookies, so there is nothing for a cross-site request to ride on.
    """

    ALLOWED_METHODS: ClassVar[str] = "GET, POST, PATCH, DELETE, OPTIONS"
    ALLOWED_HEADERS: ClassVar[str] = "authorization, content-type"
    PREFLIGHT_MAX_AGE: ClassVar[str] = "600"

    def _applies_to(self, scope: Scope) -> bool:
        return not scope["path"].startswith(PUBLIC_PATH_PREFIX)

    def _allow_origin(self, origin: str | None) -> str | None:
        # Read fresh on every request rather than captured once at app
        # construction: ConfigService's own cache (dropped by the test suite's
        # fresh_settings fixture between tests) is what makes this behave like
        # every other setting, not a special case for this one middleware.
        allowed_origins = (
            ConfigService.get_without_deps().settings.client_allowed_origins
        )
        if origin is None or origin not in allowed_origins:
            return None
        return origin

    def _preflight_headers(
        self, allowed_origin: str, request_headers: Headers
    ) -> dict[str, str]:
        return {
            ALLOW_ORIGIN: allowed_origin,
            VARY: "Origin",
            "access-control-allow-methods": self.ALLOWED_METHODS,
            "access-control-allow-headers": self.ALLOWED_HEADERS,
            "access-control-max-age": self.PREFLIGHT_MAX_AGE,
        }

    def _stamp_headers(self, headers: MutableHeaders, allowed_origin: str) -> None:
        headers[ALLOW_ORIGIN] = allowed_origin
        # `append`, not assignment: MutableHeaders.__setitem__ replaces every
        # existing value for the key, so assigning here would silently drop a
        # `Vary` the response already carries. Nothing in this service sets
        # one today, but a compression middleware or a future MCP release
        # would set `Vary: Accept-Encoding`, and dropping that in front of a
        # cache serves the wrong encoding to somebody.
        headers.append(VARY, "Origin")
