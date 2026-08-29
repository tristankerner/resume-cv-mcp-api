"""Login, and every way a JWT can fail to identify someone."""

import secrets
from datetime import UTC, datetime, timedelta
from typing import cast

import jwt
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.user import User
from services.auth.auth_service import AuthService
from services.auth.principal import CredentialKind
from services.auth.scopes import Scopes
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService


@pytest.fixture
def settings():
    return ConfigService.get_without_deps().settings


def encode(settings, claims: dict, key: str | None = None) -> str:
    return jwt.encode(
        claims,
        key or settings.auth_secret_key.get_secret_value(),
        algorithm=settings.auth_algorithm,
    )


async def authenticate(token: str | None):
    """Run the real authentication path against a token."""
    async with DatabaseService.session() as db:
        service = AuthService(db, token, ConfigService.get_without_deps())
        return await service.authenticate()


class TestLogin:
    async def test_returns_a_bearer_token(self, client, admin):
        response = await client.post(
            "/token", data={"username": admin.username, "password": admin.password}
        )
        assert response.status_code == 200
        assert response.json()["token_type"] == "bearer"

    async def test_wrong_password(self, client, admin):
        response = await client.post(
            "/token", data={"username": admin.username, "password": "not-the-password"}
        )
        assert response.status_code == 401

    async def test_unknown_user(self, client, password):
        response = await client.post(
            "/token", data={"username": "nobody-at-all", "password": password}
        )
        assert response.status_code == 401

    async def test_user_without_a_password_cannot_log_in(self, client, password):
        """Service accounts are provisioned passwordless; the hasher would
        raise rather than return False if this were not guarded."""
        async with DatabaseService.session() as db:
            db.add(User(username="passwordless", password=None, roles=[]))
            await db.commit()

        response = await client.post(
            "/token", data={"username": "passwordless", "password": password}
        )
        assert response.status_code == 401

    async def test_inactive_user_cannot_log_in(self, client, admin):
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, admin.user_id)
            assert user is not None
            user.active = False
            await db.commit()

        response = await client.post(
            "/token", data={"username": admin.username, "password": admin.password}
        )
        assert response.status_code == 401


class TestTokenSubject:
    async def test_subject_is_the_user_id_as_a_string(self, client, admin, settings):
        response = await client.post(
            "/token", data={"username": admin.username, "password": admin.password}
        )
        claims = jwt.decode(
            response.json()["access_token"],
            settings.auth_secret_key.get_secret_value(),
            algorithms=[settings.auth_algorithm],
        )
        assert claims["sub"] == str(admin.user_id)

    async def test_token_carries_no_username(self, client, admin, settings):
        """Nothing mutable in the token; identity is resolved per request."""
        response = await client.post(
            "/token", data={"username": admin.username, "password": admin.password}
        )
        claims = jwt.decode(
            response.json()["access_token"],
            settings.auth_secret_key.get_secret_value(),
            algorithms=[settings.auth_algorithm],
        )
        assert admin.username not in str(claims)


class TestAccessTokens:
    def test_default_expiry_is_applied_when_none_given(self, settings):
        # create_access_token reads configuration only, so the session the
        # constructor asks for is never touched. Cast rather than build one:
        # the production annotation is right, the exception belongs here.
        service = AuthService(
            cast(AsyncSession, None), None, ConfigService.get_without_deps()
        )
        token = service.create_access_token({"sub": "someone"})
        claims = jwt.decode(
            token,
            settings.auth_secret_key.get_secret_value(),
            algorithms=[settings.auth_algorithm],
        )
        expires_in = claims["exp"] - claims["iat"]
        assert 0 < expires_in <= timedelta(minutes=15).total_seconds()

    def test_explicit_expiry_is_honoured(self, settings):
        service = AuthService(  # unused by create_access_token; see above
            cast(AsyncSession, None), None, ConfigService.get_without_deps()
        )
        token = service.create_access_token(
            {"sub": "someone"}, expires_delta=timedelta(minutes=60)
        )
        claims = jwt.decode(
            token,
            settings.auth_secret_key.get_secret_value(),
            algorithms=[settings.auth_algorithm],
        )
        assert claims["exp"] - claims["iat"] == timedelta(minutes=60).total_seconds()


class TestAuthenticate:
    async def test_no_token_is_anonymous(self):
        assert await authenticate(None) is None
        assert await authenticate("") is None

    async def test_valid_token_yields_a_principal(self, admin):
        principal = await authenticate(admin.token)
        assert principal.username == admin.username
        assert principal.user_id == admin.user_id
        assert principal.credential is CredentialKind.JWT
        assert principal.api_key_id is None

    async def test_scopes_come_from_the_user_record(self, member):
        principal = await authenticate(member.token)
        assert principal.scopes == frozenset(Scopes) - {Scopes.USERS_ADMIN}

    async def test_role_revoked_after_issue_takes_effect_immediately(self, member):
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, member.user_id)
            assert user is not None
            user.roles = []
            await db.commit()

        principal = await authenticate(member.token)
        assert principal.scopes == frozenset()

    async def test_garbage_token(self):
        assert await authenticate("not-a-token") is None

    async def test_wrong_signature(self, settings, admin):
        forged = encode(
            settings, {"sub": str(admin.user_id)}, key=secrets.token_urlsafe(32)
        )
        assert await authenticate(forged) is None

    async def test_expired_token(self, settings, admin):
        expired = encode(
            settings,
            {
                "sub": str(admin.user_id),
                "exp": datetime.now(UTC) - timedelta(minutes=1),
            },
        )
        assert await authenticate(expired) is None

    async def test_token_without_a_subject(self, settings):
        assert await authenticate(encode(settings, {"foo": "bar"})) is None

    async def test_token_with_an_empty_subject(self, settings):
        assert await authenticate(encode(settings, {"sub": ""})) is None

    async def test_token_for_a_deleted_user(self, settings, admin):
        assert (
            await authenticate(encode(settings, {"sub": str(admin.user_id + 999)}))
            is None
        )

    @pytest.mark.parametrize("subject", ["admin-user", "not-a-number", "12.5", "1,2"])
    async def test_non_numeric_subject_is_refused(self, settings, subject):
        """Including a username, which is what the subject used to be."""
        assert await authenticate(encode(settings, {"sub": subject})) is None

    @pytest.mark.parametrize("subject", [1, True, None, ["1"], {"id": 1}])
    async def test_non_string_subject_is_refused(self, settings, subject, admin):
        """RFC 7519 wants a string. A bare `true` would otherwise int() to 1 and
        authenticate as whichever user holds that id."""
        assert await authenticate(encode(settings, {"sub": subject})) is None

    async def test_token_for_a_deactivated_user(self, admin):
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, admin.user_id)
            assert user is not None
            user.active = False
            await db.commit()

        assert await authenticate(admin.token) is None

    async def test_unknown_role_grants_nothing(self, make_actor):
        actor = await make_actor("odd-role", ["not-a-real-role"])
        principal = await authenticate(actor.token)
        assert principal.scopes == frozenset()


class TestRouteGuards:
    async def test_protected_route_rejects_anonymous(self, client):
        assert (await client.get("/users/me")).status_code == 401

    async def test_protected_route_rejects_garbage(self, client):
        response = await client.get(
            "/users/me", headers={"Authorization": "Bearer nonsense"}
        )
        assert response.status_code == 401

    async def test_challenge_header_is_present(self, client):
        response = await client.get("/users/me")
        assert response.headers["www-authenticate"] == "Bearer"
