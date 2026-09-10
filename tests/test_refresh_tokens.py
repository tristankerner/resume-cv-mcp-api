"""POST /token/refresh and POST /token/logout — the interactive session's
refresh-token grant, and its reuse-detection behaviour. Mirrors
`test_oauth.py`'s `test_refresh_rotation_and_reuse_detection_revokes_the_chain`
for the password-login side of the system.
"""

from datetime import timedelta

import jwt

from persistence.auth_refresh_token import AuthRefreshToken
from persistence.base import Clock
from persistence.user import User
from services.auth.api_keys import ApiKeyToken
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService


async def login(client, username, password) -> dict:
    response = await client.post(
        "/token", data={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestLoginIssuesARefreshToken:
    async def test_token_response_carries_a_refresh_token(
        self, client, admin, password
    ):
        body = await login(client, admin.username, password)
        assert body["refresh_token"]
        assert body["refresh_token"] != body["access_token"]


class TestRefresh:
    async def test_refresh_returns_a_new_access_and_refresh_token(
        self, client, admin, password
    ):
        first = await login(client, admin.username, password)

        response = await client.post(
            "/token/refresh", data={"refresh_token": first["refresh_token"]}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        # Not asserted different from `first["access_token"]`: both encode
        # only {sub, iat, exp} and a refresh issued in the same second as
        # login mints a byte-identical JWT. The refresh token is 32 bytes of
        # fresh randomness regardless, so that is the real assertion.
        assert body["access_token"]
        assert body["refresh_token"] != first["refresh_token"]
        assert body["token_type"] == "bearer"

    async def test_new_access_token_identifies_the_same_user(
        self, client, admin, password
    ):
        first = await login(client, admin.username, password)
        settings = ConfigService.get_with_deps().settings

        response = await client.post(
            "/token/refresh", data={"refresh_token": first["refresh_token"]}
        )
        claims = jwt.decode(
            response.json()["access_token"],
            settings.auth_secret_key.get_secret_value(),
            algorithms=[settings.auth_algorithm],
        )
        assert claims["sub"] == str(admin.user_id)

    async def test_presented_refresh_token_is_unusable_afterwards(
        self, client, admin, password
    ):
        first = await login(client, admin.username, password)

        await client.post(
            "/token/refresh", data={"refresh_token": first["refresh_token"]}
        )
        replay = await client.post(
            "/token/refresh", data={"refresh_token": first["refresh_token"]}
        )
        assert replay.status_code == 401

    async def test_reusing_a_revoked_token_revokes_the_whole_chain(
        self, client, admin, password
    ):
        first = await login(client, admin.username, password)

        rotated = await client.post(
            "/token/refresh", data={"refresh_token": first["refresh_token"]}
        )
        assert rotated.status_code == 200
        second_refresh = rotated.json()["refresh_token"]

        # Reuse of the already-rotated first token.
        replay = await client.post(
            "/token/refresh", data={"refresh_token": first["refresh_token"]}
        )
        assert replay.status_code == 401

        # The legitimate second token is dead too — the reuse revoked the
        # whole chain, not just the token presented.
        chained = await client.post(
            "/token/refresh", data={"refresh_token": second_refresh}
        )
        assert chained.status_code == 401

    async def test_unknown_token_is_refused(self, client):
        response = await client.post(
            "/token/refresh", data={"refresh_token": "not-a-real-token"}
        )
        assert response.status_code == 401

    async def test_expired_token_is_refused(self, client, admin, password):
        first = await login(client, admin.username, password)

        async with DatabaseService.session() as db:
            record = await AuthRefreshToken.get_by_hash(
                db, ApiKeyToken.hash_secret(first["refresh_token"])
            )
            assert record is not None
            record.expires_at = record.created_at - timedelta(days=1)
            await db.commit()

        response = await client.post(
            "/token/refresh", data={"refresh_token": first["refresh_token"]}
        )
        assert response.status_code == 401

    async def test_locked_account_cannot_refresh(self, client, admin, password):
        first = await login(client, admin.username, password)

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, admin.user_id)
            assert user is not None
            user.locked_until = Clock.utcnow() + timedelta(minutes=15)
            await db.commit()

        response = await client.post(
            "/token/refresh", data={"refresh_token": first["refresh_token"]}
        )
        assert response.status_code == 429

    async def test_deactivated_account_cannot_refresh(self, client, admin, password):
        first = await login(client, admin.username, password)

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, admin.user_id)
            assert user is not None
            user.active = False
            await db.commit()

        response = await client.post(
            "/token/refresh", data={"refresh_token": first["refresh_token"]}
        )
        assert response.status_code == 401

    async def test_a_deleted_users_token_is_refused(self, client, admin, password):
        first = await login(client, admin.username, password)

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, admin.user_id)
            assert user is not None
            await db.delete(user)
            await db.commit()

        response = await client.post(
            "/token/refresh", data={"refresh_token": first["refresh_token"]}
        )
        assert response.status_code == 401


class TestLogout:
    async def test_logout_returns_no_content(self, client, admin, password):
        first = await login(client, admin.username, password)

        response = await client.post(
            "/token/logout", data={"refresh_token": first["refresh_token"]}
        )
        assert response.status_code == 204

    async def test_logout_revokes_the_chain(self, client, admin, password):
        first = await login(client, admin.username, password)

        await client.post(
            "/token/logout", data={"refresh_token": first["refresh_token"]}
        )

        response = await client.post(
            "/token/refresh", data={"refresh_token": first["refresh_token"]}
        )
        assert response.status_code == 401

    async def test_logout_does_not_invalidate_the_still_live_access_token(
        self, client, admin, password
    ):
        """The access token is a stateless JWT — logout only stops the
        session from being refreshed past this point, it does not revoke
        anything already issued. See LoginService.logout's docstring."""
        first = await login(client, admin.username, password)

        await client.post(
            "/token/logout", data={"refresh_token": first["refresh_token"]}
        )

        response = await client.get(
            "/users/me",
            headers={"Authorization": f"Bearer {first['access_token']}"},
        )
        assert response.status_code == 200

    async def test_logout_with_an_unknown_token_is_silently_fine(self, client):
        """RFC 7009-style idempotence: revoking a token that does not exist
        is not an error, and does not disclose whether it ever did."""
        response = await client.post(
            "/token/logout", data={"refresh_token": "not-a-real-token"}
        )
        assert response.status_code == 204

    async def test_logout_twice_is_fine(self, client, admin, password):
        first = await login(client, admin.username, password)

        first_logout = await client.post(
            "/token/logout", data={"refresh_token": first["refresh_token"]}
        )
        second_logout = await client.post(
            "/token/logout", data={"refresh_token": first["refresh_token"]}
        )
        assert first_logout.status_code == 204
        assert second_logout.status_code == 204


class TestMfaLoginIssuesARefreshToken:
    async def test_completing_mfa_also_returns_a_refresh_token(
        self, client, enrolled, totp_code
    ):
        from tests.test_mfa_login import start_login

        step_one = await start_login(
            client, enrolled.actor.username, enrolled.actor.password
        )
        mfa_token = step_one.json()["mfa_token"]
        response = await client.post(
            "/token/mfa",
            data={"mfa_token": mfa_token, "code": totp_code(enrolled.secret)},
        )
        assert response.status_code == 200
        assert response.json()["refresh_token"]
