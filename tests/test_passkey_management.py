"""The self-service /users/me/passkeys routes, and the admin reset route."""

from persistence.user import User
from services.auth.scopes import Scopes
from services.config.config_service import ConfigService
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


async def request_options(client, actor, password: str):
    return await client.post(
        "/users/me/passkeys/options",
        headers=actor.headers,
        json={"current_password": password},
    )


class TestRegistrationOptions:
    async def test_the_correct_password_is_accepted(self, client, roleless, password):
        response = await request_options(client, roleless, password)
        assert response.status_code == 200
        body = response.json()
        assert body["options"]["authenticatorSelection"]["residentKey"] == "required"
        assert body["registration_token"]
        assert body["expires_in"] > 0

    async def test_a_wrong_password_is_refused(self, client, roleless):
        response = await request_options(client, roleless, "not-the-password")
        assert response.status_code == 403

    async def test_a_wrong_password_is_charged_to_the_throttle(self, client, roleless):
        await request_options(client, roleless, "not-the-password")
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, roleless.user_id)
            assert user is not None
            assert user.failed_login_count == 1


class TestPasskeyManagementFlow:
    """Full registration -> list -> rename -> delete, driven through HTTP —
    modelled on tests/test_mfa_management.py's flow tests."""

    async def test_register_then_list_shows_the_flags_the_authenticator_set(
        self, client, passkey_actor
    ):
        response = await client.get(
            "/users/me/passkeys", headers=passkey_actor.actor.headers
        )
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert len(body["credentials"]) == 1
        credential = body["credentials"][0]
        assert credential["device_type"] == "multi_device"
        assert credential["backed_up"] is True
        assert credential["label"] == "Test passkey"

    async def test_registering_the_same_credential_id_twice_is_409(
        self, client, passkey_actor, password
    ):
        actor = passkey_actor.actor
        options_response = await request_options(client, actor, password)
        body = options_response.json()

        # Re-registering the same authenticator produces the same credential
        # id, which the unique index on passkey_credentials.credential_id
        # refuses on the second insert.
        credential = passkey_actor.authenticator.register(body["options"])
        response = await client.post(
            "/users/me/passkeys",
            headers=actor.headers,
            json={
                "registration_token": body["registration_token"],
                "label": "Duplicate",
                "credential": credential,
            },
        )
        assert response.status_code == 409

    async def test_the_credential_cap_refuses_the_eleventh(
        self, client, make_actor, password, authenticator
    ):
        actor = await make_actor("passkey-cap-user", [])
        for i in range(10):
            options_response = await request_options(client, actor, password)
            assert options_response.status_code == 200, options_response.text
            body = options_response.json()
            device = authenticator()
            credential = device.register(body["options"])
            response = await client.post(
                "/users/me/passkeys",
                headers=actor.headers,
                json={
                    "registration_token": body["registration_token"],
                    "label": f"Key {i}",
                    "credential": credential,
                },
            )
            assert response.status_code == 200, response.text

        eleventh_options = await request_options(client, actor, password)
        assert eleventh_options.status_code == 409

    async def test_rename_works_without_a_password(self, client, passkey_actor):
        actor = passkey_actor.actor
        listed = await client.get("/users/me/passkeys", headers=actor.headers)
        credential_id = listed.json()["credentials"][0]["id"]

        response = await client.patch(
            f"/users/me/passkeys/{credential_id}",
            headers=actor.headers,
            json={"label": "Renamed"},
        )
        assert response.status_code == 200
        assert response.json()["label"] == "Renamed"

    async def test_renaming_another_users_credential_is_404(
        self, client, passkey_actor, admin
    ):
        actor = passkey_actor.actor
        listed = await client.get("/users/me/passkeys", headers=actor.headers)
        credential_id = listed.json()["credentials"][0]["id"]

        response = await client.patch(
            f"/users/me/passkeys/{credential_id}",
            headers=admin.headers,
            json={"label": "Stolen"},
        )
        assert response.status_code == 404

    async def test_delete_requires_the_password(self, client, passkey_actor):
        actor = passkey_actor.actor
        listed = await client.get("/users/me/passkeys", headers=actor.headers)
        credential_id = listed.json()["credentials"][0]["id"]

        wrong = await client.request(
            "DELETE",
            f"/users/me/passkeys/{credential_id}",
            headers=actor.headers,
            json={"current_password": "not-the-password"},
        )
        assert wrong.status_code == 403

        response = await client.request(
            "DELETE",
            f"/users/me/passkeys/{credential_id}",
            headers=actor.headers,
            json={"current_password": actor.password},
        )
        assert response.status_code == 204

        listed_after = await client.get("/users/me/passkeys", headers=actor.headers)
        assert listed_after.json()["credentials"] == []

    async def test_deleting_another_users_credential_is_404(
        self, client, passkey_actor, admin
    ):
        actor = passkey_actor.actor
        listed = await client.get("/users/me/passkeys", headers=actor.headers)
        credential_id = listed.json()["credentials"][0]["id"]

        response = await client.request(
            "DELETE",
            f"/users/me/passkeys/{credential_id}",
            headers=admin.headers,
            json={"current_password": admin.password},
        )
        assert response.status_code == 404

    async def test_deleting_an_unknown_credential_is_404(
        self, client, roleless, password
    ):
        response = await client.request(
            "DELETE",
            "/users/me/passkeys/999999",
            headers=roleless.headers,
            json={"current_password": password},
        )
        assert response.status_code == 404


class TestAuthorization:
    async def test_an_api_key_is_refused_on_get(self, client, member):
        headers = await narrowed_api_key(client, member, Scopes.RESUME_READ)
        response = await client.get("/users/me/passkeys", headers=headers)
        assert response.status_code == 403

    async def test_an_api_key_is_refused_on_options(self, client, member, password):
        headers = await narrowed_api_key(client, member, Scopes.RESUME_READ)
        response = await client.post(
            "/users/me/passkeys/options",
            headers=headers,
            json={"current_password": password},
        )
        assert response.status_code == 403

    async def test_an_oauth_token_is_refused_on_get(self, client, roleless):
        headers = await oauth_headers(roleless)
        response = await client.get("/users/me/passkeys", headers=headers)
        assert response.status_code == 403

    async def test_get_requires_no_password(self, client, roleless):
        response = await client.get("/users/me/passkeys", headers=roleless.headers)
        assert response.status_code == 200


class TestEmptyStatus:
    async def test_a_fresh_account_has_no_passkeys_but_the_feature_is_enabled(
        self, client, roleless
    ):
        response = await client.get("/users/me/passkeys", headers=roleless.headers)
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert body["credentials"] == []


class TestAdminReset:
    async def test_reset_passkeys_requires_users_admin(self, client, passkey_actor):
        response = await client.delete(
            f"/users/{passkey_actor.actor.user_id}/passkeys",
            headers=passkey_actor.actor.headers,
        )
        assert response.status_code == 403

    async def test_reset_passkeys_removes_every_credential(
        self, client, passkey_actor, admin
    ):
        response = await client.delete(
            f"/users/{passkey_actor.actor.user_id}/passkeys",
            headers=admin.headers,
        )
        assert response.status_code == 204

        listed = await client.get(
            "/users/me/passkeys", headers=passkey_actor.actor.headers
        )
        assert listed.json()["credentials"] == []

    async def test_reset_passkeys_is_idempotent_on_an_account_with_none(
        self, client, roleless, admin
    ):
        response = await client.delete(
            f"/users/{roleless.user_id}/passkeys", headers=admin.headers
        )
        assert response.status_code == 204

    async def test_reset_passkeys_needs_an_interactive_login(
        self, client, passkey_actor, admin
    ):
        headers = await narrowed_api_key(client, admin, Scopes.USERS_ADMIN)
        response = await client.delete(
            f"/users/{passkey_actor.actor.user_id}/passkeys", headers=headers
        )
        assert response.status_code == 403


class TestDisabledDeployment:
    async def test_options_404_when_webauthn_rp_id_is_unset(
        self, client, roleless, password, monkeypatch
    ):
        monkeypatch.setenv("WEBAUTHN_RP_ID", "")
        monkeypatch.setenv("WEBAUTHN_ALLOWED_ORIGINS", "")
        ConfigService.reset()

        response = await request_options(client, roleless, password)
        assert response.status_code == 404

    async def test_list_still_works_and_reports_disabled(
        self, client, roleless, monkeypatch
    ):
        monkeypatch.setenv("WEBAUTHN_RP_ID", "")
        monkeypatch.setenv("WEBAUTHN_ALLOWED_ORIGINS", "")
        ConfigService.reset()

        response = await client.get("/users/me/passkeys", headers=roleless.headers)
        assert response.status_code == 200
        assert response.json()["enabled"] is False
