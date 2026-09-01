from __future__ import annotations

from typing import Annotated, ClassVar
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.user import User
from services.auth.signed_token import SignedToken
from services.config.config_service import ConfigService, ConfigServiceModel
from services.database.database_service import DatabaseService


class DocsSessionToken(SignedToken):
    """The signed value in the `docs_session` cookie.

    A JWT and not an opaque id for the same reason the MFA challenge is one:
    there is nothing to revoke over an hour, and a stateless value costs the
    documentation no table and no sweep.

    Grants exactly one thing — reading the route index — and is bound to the
    password hash in force when it was issued, so a password change ends
    every docs session against the old one.
    """

    TOKEN_USE: ClassVar[str] = "docs_session"
    COOKIE_NAME: ClassVar[str] = "docs_session"

    @classmethod
    def mint(cls, settings: ConfigServiceModel, user: User) -> tuple[str, int]:
        return cls._encode(
            settings,
            {"sub": str(user.id), "pwb": cls.password_binding(user)},
            settings.docs_session_minutes,
        )

    @classmethod
    async def user_from_cookie(
        cls, db: AsyncSession, settings: ConfigServiceModel, token: str
    ) -> User | None:
        """The user named by a valid, unexpired session cookie, or None for
        any failure at all — including a deactivated account or a password
        changed since the cookie was issued."""
        payload = cls._decode(settings, token)
        if payload is None:
            return None
        subject = payload.get("sub")
        if not isinstance(subject, str):
            return None
        try:
            user_id = int(subject)
        except ValueError:
            return None

        user = await User.get_user_by_id(db, user_id)
        if user is None or not user.active:
            return None
        if payload.get("pwb") != cls.password_binding(user):
            return None
        return user


class DocsAccessGuard:
    """Gate the documentation on a signed session cookie, in production.

    Development waves everyone through, for the reason the docstring this
    replaces gave: a login prompt in front of localhost only trains people
    to type one. In production, no valid cookie means a 303 to the login
    page rather than a 401 — this is a page a browser navigates to, not an
    API credential a client retries with.
    """

    @staticmethod
    async def check(
        request: Request,
        db: Annotated[AsyncSession, Depends(DatabaseService.get_async_db_session)],
        config_service: Annotated[ConfigService, Depends(ConfigService.get_with_deps)],
    ) -> None:
        settings = config_service.settings
        if not settings.is_production:
            return

        token = request.cookies.get(DocsSessionToken.COOKIE_NAME)
        user = (
            await DocsSessionToken.user_from_cookie(db, settings, token)
            if token
            else None
        )
        if user is not None:
            return

        # Slashes left unescaped: `request.url.path` is always one of the four
        # docs paths this guard covers, never attacker-supplied content.
        next_path = quote(request.url.path, safe="/")
        raise HTTPException(
            status_code=303, headers={"Location": f"/docs/login?next={next_path}"}
        )
