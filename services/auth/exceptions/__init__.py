from collections.abc import Iterable

from fastapi import HTTPException, status


class AuthErrors:
    @staticmethod
    def credentials() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @staticmethod
    def api_key_not_found() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="API key not found"
        )

    @staticmethod
    def requires_interactive_login() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "API keys can only be managed with an interactive login, not with "
                "another API key."
            ),
        )

    @staticmethod
    def wrong_current_password() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Current password is incorrect.",
        )

    @classmethod
    def account_locked_permanently(cls) -> HTTPException:
        """A permanent lock is deliberately indistinguishable from a wrong
        password: unlike a temporary one there is no wait to communicate, so
        the only thing a distinct response would convey is that the account
        exists and is worth queueing up for the moment an admin unlocks it."""
        return cls.credentials()

    @staticmethod
    def account_locked(retry_after_seconds: int) -> HTTPException:
        """429 with the wait, for an account or address that is temporarily locked.

        Says plainly that the account exists, which is the trade taken here:
        the login path already discloses that much through its timing — a
        missing user returns before any hash is computed — so withholding it
        buys nothing, and a locked-out owner who is told to come back in
        twelve minutes does not spend those minutes retyping a password that
        is already correct.
        """
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(retry_after_seconds)},
        )

    @staticmethod
    def scopes_exceed_owner(scopes: list[str]) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Key would grant scopes its owner does not hold: "
                + ", ".join(sorted(scopes))
            ),
        )

    @staticmethod
    def insufficient_scope(scope: str) -> HTTPException:
        """403 rather than 401: the caller authenticated, they just may not
        do this."""
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires scope: {scope}",
            headers={
                "WWW-Authenticate": (
                    f'Bearer error="insufficient_scope", scope="{scope}"'
                )
            },
        )

    @staticmethod
    def insufficient_any_scope(scopes: Iterable[str]) -> HTTPException:
        """The same refusal where any one of several scopes would have done.

        The listing route is the case: it spans every document type, and a
        caller holding one read scope should see that type rather than be
        refused for the two they lack. RFC 6750 already defines the `scope`
        challenge attribute as a space-delimited list, so naming all of them
        is what that header is for.
        """
        listed = sorted(scopes)
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires one of these scopes: " + ", ".join(listed),
            headers={
                "WWW-Authenticate": (
                    f'Bearer error="insufficient_scope", scope="{" ".join(listed)}"'
                )
            },
        )

    @staticmethod
    def insufficient_all_scopes(scopes: Iterable[str]) -> HTTPException:
        """The same refusal where every one of several scopes is required.
        `GET /applications/{id}/contact-options` is the case: it returns
        contact PII keyed off an application, so both `applications:read`
        and `contacts:read` apply, unlike `insufficient_any_scope`'s "any one
        would do"."""
        listed = sorted(scopes)
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires all of these scopes: " + ", ".join(listed),
            headers={
                "WWW-Authenticate": (
                    f'Bearer error="insufficient_scope", scope="{" ".join(listed)}"'
                )
            },
        )


class MfaErrors:
    """Second-factor refusals. See AuthErrors for the same one-static-method-
    per-refusal style."""

    @staticmethod
    def invalid_code() -> HTTPException:
        """Used for a wrong second factor, but never for a wrong password —
        the two must stay indistinguishable to an unauthenticated caller, so
        the login path raises AuthErrors.credentials() instead of this."""
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="That code is not valid."
        )

    @staticmethod
    def challenge_expired() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The login attempt expired. Start again.",
        )

    @staticmethod
    def credential_not_found() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="MFA method not found"
        )

    @staticmethod
    def already_activated() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That method is already active.",
        )

    @staticmethod
    def activation_not_required() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This method needs no activation step.",
        )

    @staticmethod
    def wrong_kind() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That method does not take an activation code.",
        )

    @staticmethod
    def too_many_credentials() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have the maximum number of MFA methods.",
        )
