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

`OAuthClientAdminErrors` at the bottom is none of the above: `/oauth-clients`
is an ordinary admin JSON API, not part of the RFC surface, and answers in the
app's usual `{"detail": ...}` shape via a plain HTTPException.
"""

from __future__ import annotations

from fastapi import HTTPException, status


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


class AuthorizeMfaRequired(Exception):
    """The password was right and a second factor is outstanding.

    Its own exception rather than a variant of AuthorizeLoginFailed because
    the router answers it with a different page — the protocol parameters
    have to survive the extra round trip in hidden fields, and the user must
    not be asked for their password a second time. `detail` is None for the
    first challenge (render at 200) and set for a rejected code (render at
    401, with a freshly minted token so the next attempt gets a full window).
    """

    def __init__(self, mfa_token: str, detail: str | None = None):
        self.mfa_token = mfa_token
        self.detail = detail
        super().__init__(detail or "MFA required")


class OAuthClientAdminErrors:
    """Refusals for the admin client-management routes. Ordinary
    HTTPExceptions, unlike everything above — see the module docstring."""

    @staticmethod
    def not_found() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="OAuth client not found"
        )

    @staticmethod
    def invalid_request(detail: str) -> HTTPException:
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
