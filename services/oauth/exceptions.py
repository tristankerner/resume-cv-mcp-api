"""Errors for the three new machine-to-machine endpoints, and for the one
browser-facing one.

`/oauth/register`, `/oauth/token` and `/oauth/revoke` answer in the flat RFC
6749 §5.2 / RFC 7591 §3.2.2 shape — `{"error": ..., "error_description": ...}`
— which is not the app's usual `{"detail": ...}`. `OAuthError` carries enough
to build that response; the router builds it, so persistence and service code
never import Starlette.

`/oauth/authorize` is different: a redirect-based flow answers a validated
request with a redirect carrying `error=`, and an unvalidated one with an
HTML page — never a redirect to a URI that was never checked. Two exception
types for the two outcomes.
"""

from __future__ import annotations


class OAuthError(Exception):
    """A JSON-error response from /oauth/register, /oauth/token or
    /oauth/revoke."""

    def __init__(self, status_code: int, error: str, description: str | None = None):
        self.status_code = status_code
        self.error = error
        self.description = description
        super().__init__(f"{error}: {description}" if description else error)


class OAuthErrors:
    @staticmethod
    def invalid_redirect_uri(detail: str) -> OAuthError:
        return OAuthError(400, "invalid_redirect_uri", detail)

    @staticmethod
    def invalid_client_metadata(detail: str) -> OAuthError:
        return OAuthError(400, "invalid_client_metadata", detail)

    @staticmethod
    def invalid_client() -> OAuthError:
        return OAuthError(401, "invalid_client", "Unknown client or bad client secret.")

    @staticmethod
    def invalid_grant(detail: str) -> OAuthError:
        return OAuthError(400, "invalid_grant", detail)

    @staticmethod
    def unsupported_grant_type(detail: str) -> OAuthError:
        return OAuthError(400, "unsupported_grant_type", detail)


class AuthorizeRedirectError(Exception):
    """A protocol error against an already-validated client and redirect_uri.

    Delivered as a redirect carrying `error` and `error_description`, per RFC
    6749 §4.1.2.1 — the redirect_uri is trusted at this point, so answering
    the caller there is correct rather than a JSON body or an HTML page.
    """

    def __init__(self, error: str, description: str, state: str | None = None):
        self.error = error
        self.description = description
        self.state = state
        super().__init__(f"{error}: {description}")


class AuthorizeFatalError(Exception):
    """client_id or redirect_uri could not be validated.

    Rendered as an HTML error page, never a redirect — redirecting here would
    send the browser to a URI that was never checked, which is the exact
    failure this endpoint exists to prevent.
    """

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class AuthorizeLoginFailed(Exception):
    """Wrong username or password. Re-show the form; this is not a protocol
    error, so it is neither a redirect nor the fatal-error page."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)
