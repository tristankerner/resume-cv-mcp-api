"""Cross-origin access to the public document routes.

The résumé page is a separate site on a separate origin, so a browser will
fetch `/public/...` and then refuse to hand the response to the page unless it
carries `Access-Control-Allow-Origin`. Nothing else in this service needs the
header, and giving it to anything else would be a mistake — see below.
"""

from typing import ClassVar

from starlette.datastructures import Headers
from starlette.types import Scope

from middleware.cors_base import CorsMiddleware

PUBLIC_PATH_PREFIX = "/public/"

ALLOW_ORIGIN = "access-control-allow-origin"


class PublicCorsMiddleware(CorsMiddleware):
    """Mark `/public/*` responses readable by any browser origin.

    Three decisions here are coupled to how this is deployed.

    **A literal `*`, not the caller's origin.** An allowlist would have to echo
    `Origin` back and set `Vary: Origin`; a CDN that caches these responses
    without varying on it would hand whichever origin arrived first to everyone
    else. Restricting origins buys nothing anyway — these routes already serve
    unauthenticated to `curl`, and CORS constrains browsers, not callers.

    **Unconditional, not only when `Origin` is present.** Starlette's
    `CORSMiddleware` returns early on a request with no `Origin`, which behind
    a cache that does not key on it means one credential-free `curl` — a smoke
    test, a monitor, a warm-up — fills the entry with a header-less copy and
    the front end fails for the rest of the TTL.

    **Scoped to the public prefix.** Everything else authenticates, and there
    is no reason to advertise those routes as browser-readable.
    """

    ALLOWED_METHODS: ClassVar[str] = "GET, HEAD, OPTIONS"
    # A day. Preflights are answered here rather than at the edge, so the
    # browser's own cache is the only thing keeping them off the origin.
    PREFLIGHT_MAX_AGE: ClassVar[str] = "86400"

    def _applies_to(self, scope: Scope) -> bool:
        return scope["path"].startswith(PUBLIC_PATH_PREFIX)

    def _allow_origin(self, origin: str | None) -> str:
        return "*"

    def _preflight_headers(
        self, allowed_origin: str, request_headers: Headers
    ) -> dict[str, str]:
        """Answer the preflight permissively.

        No preflight fires for the request this exists to serve — a plain GET
        with no custom headers is a simple request — so this is here for the
        day the front end adds one. Being generous costs nothing: there are no
        credentials to attach.
        """
        headers = {
            ALLOW_ORIGIN: allowed_origin,
            "access-control-allow-methods": self.ALLOWED_METHODS,
            "access-control-max-age": self.PREFLIGHT_MAX_AGE,
        }
        requested = request_headers.get("access-control-request-headers")
        if requested:
            headers["access-control-allow-headers"] = requested
        return headers
