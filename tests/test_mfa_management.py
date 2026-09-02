"""The self-service /users/me/mfa routes."""

import pyotp

from persistence.user import User
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService
from tests.helpers import OAuthTokens


async def narrowed_api_key(client, actor, *scopes: Scopes) -> dict[str, str]:
    created = await client.post(
        "/api-keys",
        headers=actor.headers,
        json={"name": "narrowed", "scopes": [scope.value for scope in scopes]},
    )
    assert created.status_code == 200, created.text
    return {"Authorization": f"Bearer {created.json()['key']}"}


async def oauth_headers(actor, *scopes: Scopes) -> dict[str, str]:
    return await OAuthTokens.headers(actor, *scopes)


async def enroll_totp(client, actor, password: str, label: str = "Phone"):
    return await client.post(
        "/users/me/mfa/totp",
        headers=actor.headers,
        json={"label": label, "current_password": password},
    )


async def activate_totp(client, actor, credential_id: int, secret: str):
    return await client.post(
        f"/users/me/mfa/totp/{credential_id}/activate",
        headers=actor.headers,
        json={"code": pyotp.TOTP(secret).now()},
    )


class TestEnrollAndActivate:
    async def test_enroll_then_activate_makes_the_credential_active(
        self, client, roleless, password
    ):
        enrolled = await enroll_totp(client, roleless, password)
        assert enrolled.status_code == 200, enrolled.text
        body = enrolled.json()
        assert body["secret"]
        assert body["otpauth_uri"].startswith("otpauth://totp/")

        activated = await activate_totp(
            client, roleless, body["credential_id"], body["secret"]
        )
        assert activated.status_code == 204

        status = await client.get("/users/me/mfa", headers=roleless.headers)
        assert status.status_code == 200
        credential = status.json()["credentials"][0]
        assert credential["activated_at"] is not None

        me = await client.get("/users/me", headers=roleless.headers)
        assert me.json()["mfa_enrolled"] is True

    async def test_an_unactivated_credential_does_not_demand_a_second_factor(
        self, client, roleless, password
    ):
        enrolled = await enroll_totp(client, roleless, password)
        assert enrolled.status_code == 200

        response = await client.post(
            "/token", data={"username": roleless.username, "password": password}
        )
        assert response.status_code == 200
        assert "access_token" in response.json()

    async def test_a_blank_label_is_refused(self, client, roleless, password):
        response = await enroll_totp(client, roleless, password, label="   ")
        assert response.status_code == 422

    async def test_activation_refuses_a_bad_code(self, client, roleless, password):
        enrolled = await enroll_totp(client, roleless, password)
        response = await client.post(
            f"/users/me/mfa/totp/{enrolled.json()['credential_id']}/activate",
            headers=roleless.headers,
            json={"code": "000000"},
        )
        assert response.status_code == 401

    async def test_a_bad_activation_code_does_not_charge_the_login_throttle(
        self, client, roleless, password
    ):
        enrolled = await enroll_totp(client, roleless, password)
        await client.post(
            f"/users/me/mfa/totp/{enrolled.json()['credential_id']}/activate",
            headers=roleless.headers,
            json={"code": "000000"},
        )

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, roleless.user_id)
            assert user is not None
            assert user.failed_login_count == 0

    async def test_the_credential_cap_refuses_the_eleventh(
        self, client, roleless, password
    ):
        for i in range(10):
            response = await enroll_totp(client, roleless, password, label=f"Key {i}")
            assert response.status_code == 200, response.text

        eleventh = await enroll_totp(client, roleless, password, label="Key 11")
        assert eleventh.status_code == 409


class TestBackupCodes:
    async def test_regenerating_returns_a_fresh_set(self, client, roleless, password):
        response = await client.post(
            "/users/me/mfa/backup-codes",
            headers=roleless.headers,
            json={"current_password": password},
        )
        assert response.status_code == 200
        codes = response.json()["codes"]
        assert len(codes) == 10
        assert len(set(codes)) == 10


class TestStatus:
    async def test_never_returns_a_secret_an_uri_or_a_backup_code(
        self, client, roleless, password
    ):
        enrolled = await enroll_totp(client, roleless, password)
        await activate_totp(
            client,
            roleless,
            enrolled.json()["credential_id"],
            enrolled.json()["secret"],
        )
        backup = await client.post(
            "/users/me/mfa/backup-codes",
            headers=roleless.headers,
            json={"current_password": password},
        )

        status = await client.get("/users/me/mfa", headers=roleless.headers)
        raw = status.text
        assert enrolled.json()["secret"] not in raw
        assert "otpauth://" not in raw
        for code in backup.json()["codes"]:
            assert code not in raw

    async def test_empty_status_for_a_fresh_account(self, client, roleless):
        status = await client.get("/users/me/mfa", headers=roleless.headers)
        assert status.status_code == 200
        body = status.json()
        assert body["enrolled"] is False
        assert body["credentials"] == []
        assert {m["kind"] for m in body["available_methods"]} == {
            "totp",
            "backup_codes",
        }


class TestAuthorization:
    async def test_an_api_key_is_refused_on_get(self, client, member, password):
        headers = await narrowed_api_key(client, member, Scopes.RESUME_READ)
        response = await client.get("/users/me/mfa", headers=headers)
        assert response.status_code == 403

    async def test_an_api_key_is_refused_on_enroll(self, client, member, password):
        headers = await narrowed_api_key(client, member, Scopes.RESUME_READ)
        response = await client.post(
            "/users/me/mfa/totp",
            headers=headers,
            json={"label": "Phone", "current_password": password},
        )
        assert response.status_code == 403

    async def test_an_oauth_token_is_refused_on_get(self, client, roleless):
        headers = await oauth_headers(roleless)
        response = await client.get("/users/me/mfa", headers=headers)
        assert response.status_code == 403

    async def test_wrong_current_password_refuses_enrollment(
        self, client, roleless, password
    ):
        response = await client.post(
            "/users/me/mfa/totp",
            headers=roleless.headers,
            json={"label": "Phone", "current_password": "not-the-password"},
        )
        assert response.status_code == 403

    async def test_wrong_current_password_is_charged_to_the_throttle(
        self, client, roleless, password
    ):
        await client.post(
            "/users/me/mfa/totp",
            headers=roleless.headers,
            json={"label": "Phone", "current_password": "not-the-password"},
        )
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, roleless.user_id)
            assert user is not None
            assert user.failed_login_count == 1

    async def test_wrong_current_password_refuses_backup_code_regeneration(
        self, client, roleless
    ):
        response = await client.post(
            "/users/me/mfa/backup-codes",
            headers=roleless.headers,
            json={"current_password": "not-the-password"},
        )
        assert response.status_code == 403

    async def test_wrong_current_password_refuses_removal(
        self, client, roleless, password
    ):
        enrolled = await enroll_totp(client, roleless, password)
        response = await client.request(
            "DELETE",
            f"/users/me/mfa/{enrolled.json()['credential_id']}",
            headers=roleless.headers,
            json={"current_password": "not-the-password"},
        )
        assert response.status_code == 403

    async def test_get_requires_no_password(self, client, roleless):
        response = await client.get("/users/me/mfa", headers=roleless.headers)
        assert response.status_code == 200


class TestRemoval:
    async def test_removing_a_credential_belonging_to_another_user_is_404(
        self, client, roleless, admin, password
    ):
        enrolled = await enroll_totp(client, roleless, password)
        response = await client.request(
            "DELETE",
            f"/users/me/mfa/{enrolled.json()['credential_id']}",
            headers=admin.headers,
            json={"current_password": admin.password},
        )
        assert response.status_code == 404

    async def test_removing_an_unknown_credential_is_404(
        self, client, roleless, password
    ):
        response = await client.request(
            "DELETE",
            "/users/me/mfa/999999",
            headers=roleless.headers,
            json={"current_password": password},
        )
        assert response.status_code == 404

    async def test_removing_the_last_method_succeeds_and_clears_mfa_enrolled(
        self, client, roleless, password
    ):
        enrolled = await enroll_totp(client, roleless, password)
        await activate_totp(
            client,
            roleless,
            enrolled.json()["credential_id"],
            enrolled.json()["secret"],
        )

        me_before = await client.get("/users/me", headers=roleless.headers)
        assert me_before.json()["mfa_enrolled"] is True

        response = await client.request(
            "DELETE",
            f"/users/me/mfa/{enrolled.json()['credential_id']}",
            headers=roleless.headers,
            json={"current_password": password},
        )
        assert response.status_code == 204

        me_after = await client.get("/users/me", headers=roleless.headers)
        assert me_after.json()["mfa_enrolled"] is False

        status = await client.get("/users/me/mfa", headers=roleless.headers)
        assert status.json()["credentials"] == []
