"""The /token passkey surface: options, login, and the throttling rules in
§5.7 of the plan — charged to the address, never the account, with a
temporary lock let through and a permanent one refused.
"""

import pyotp

from persistence.base import Base64Url, Clock
from persistence.passkey_credential import PasskeyCredential
from persistence.user import User
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService
from tests.support.webauthn_authenticator import SoftwareAuthenticator


async def passkey_options(client, username: str | None = None):
    return await client.post("/token/passkey/options", json={"username": username})


async def passkey_login(client, login_token: str, credential: dict):
    return await client.post(
        "/token/passkey",
        json={"login_token": login_token, "credential": credential},
    )


async def login_with_passkey(client, passkey_actor, *, named: bool = True):
    """The full round trip for the fixture's own actor: request options named
    by the actor's own username (or, with `named=False`, the usernameless
    discoverable-credential flow), assert with its authenticator, and
    submit."""
    username = passkey_actor.actor.username if named else None
    options_response = await passkey_options(client, username)
    assert options_response.status_code == 200, options_response.text
    body = options_response.json()
    credential = passkey_actor.authenticator.authenticate(
        body["options"], user_handle=passkey_actor.user_handle
    )
    return await passkey_login(client, body["login_token"], credential)


class TestBasicLogin:
    async def test_register_then_login_returns_a_working_refresh_token(
        self, client, passkey_actor
    ):
        response = await login_with_passkey(client, passkey_actor)
        assert response.status_code == 200
        body = response.json()
        assert "access_token" in body
        assert body["token_type"] == "bearer"

        refreshed = await client.post(
            "/token/refresh", data={"refresh_token": body["refresh_token"]}
        )
        assert refreshed.status_code == 200
        assert "access_token" in refreshed.json()

    async def test_an_enrolled_totp_account_is_not_asked_for_a_code(
        self, client, passkey_actor, password
    ):
        """A passkey login is a complete login on its own — see
        services/auth/passkeys/login.py. Enrolling TOTP on top of a passkey
        must not make the passkey path start demanding a code."""
        actor = passkey_actor.actor
        enroll = await client.post(
            "/users/me/mfa/totp",
            headers=actor.headers,
            json={"label": "Phone", "current_password": password},
        )
        assert enroll.status_code == 200, enroll.text

        activate = await client.post(
            f"/users/me/mfa/totp/{enroll.json()['credential_id']}/activate",
            headers=actor.headers,
            json={"code": pyotp.TOTP(enroll.json()["secret"]).now()},
        )
        assert activate.status_code == 204

        response = await login_with_passkey(client, passkey_actor)
        assert response.status_code == 200
        assert "access_token" in response.json()
        assert "mfa_required" not in response.json()


class TestUsernameless:
    async def test_no_username_still_logs_in_via_the_user_handle(
        self, client, passkey_actor
    ):
        response = await login_with_passkey(client, passkey_actor, named=False)
        assert response.status_code == 200
        assert "access_token" in response.json()

    async def test_an_unknown_username_gets_200_with_no_allow_credentials(self, client):
        response = await passkey_options(client, "no-such-user-at-all")
        assert response.status_code == 200
        assert not response.json()["options"].get("allowCredentials")


class TestCrossAccountRefusals:
    async def test_a_challenge_for_one_account_is_refused_with_anothers_credential(
        self, client, passkey_actor, make_actor, password, authenticator
    ):
        other_actor = await make_actor("other-passkey-user", [])
        options = await client.post(
            "/users/me/passkeys/options",
            headers=other_actor.headers,
            json={"current_password": password},
        )
        other_device = authenticator()
        other_credential_response = other_device.register(options.json()["options"])
        registered = await client.post(
            "/users/me/passkeys",
            headers=other_actor.headers,
            json={
                "registration_token": options.json()["registration_token"],
                "label": "Other",
                "credential": other_credential_response,
            },
        )
        assert registered.status_code == 200, registered.text

        # Options named for `passkey_actor`'s account (sub bound to it), but
        # answered with `other_actor`'s credential.
        options_response = await passkey_options(client, passkey_actor.actor.username)
        assertion = other_device.authenticate(options_response.json()["options"])
        response = await passkey_login(
            client, options_response.json()["login_token"], assertion
        )
        assert response.status_code == 401

    async def test_an_assertion_naming_another_accounts_user_handle_is_refused(
        self, client, passkey_actor, make_actor
    ):
        other_actor = await make_actor("handle-mismatch-user", [])
        async with DatabaseService.session() as db:
            other_user = await User.get_user_by_id(db, other_actor.user_id)
            assert other_user is not None
            handle_str = User.ensure_webauthn_handle(other_user)
            await db.commit()
            other_handle = Base64Url.decode(handle_str)

        response = await login_with_passkey(client, passkey_actor, named=False)
        # Sanity: the fixture's own handle logs in fine before we try a wrong one.
        assert response.status_code == 200

        options_response = await passkey_options(client, None)
        wrong_assertion = passkey_actor.authenticator.authenticate(
            options_response.json()["options"], user_handle=other_handle
        )
        forged = await passkey_login(
            client, options_response.json()["login_token"], wrong_assertion
        )
        assert forged.status_code == 401


class TestReplay:
    async def test_a_replayed_assertion_is_refused(self, client, make_actor, password):
        actor = await make_actor("replay-passkey-user", [])
        device = SoftwareAuthenticator(
            rp_id="testserver", origin="http://testserver", increment_sign_count=True
        )
        options = await client.post(
            "/users/me/passkeys/options",
            headers=actor.headers,
            json={"current_password": password},
        )
        registration = device.register(options.json()["options"])
        registered = await client.post(
            "/users/me/passkeys",
            headers=actor.headers,
            json={
                "registration_token": options.json()["registration_token"],
                "label": "Replay",
                "credential": registration,
            },
        )
        assert registered.status_code == 200, registered.text

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, actor.user_id)
            assert user is not None
            assert user.webauthn_user_handle is not None
            handle = Base64Url.decode(user.webauthn_user_handle)

        options_response = await passkey_options(client, actor.username)
        assertion = device.authenticate(
            options_response.json()["options"], user_handle=handle
        )

        first = await passkey_login(
            client, options_response.json()["login_token"], assertion
        )
        assert first.status_code == 200

        second_options = await passkey_options(client, actor.username)
        replayed = await passkey_login(
            client, second_options.json()["login_token"], assertion
        )
        assert replayed.status_code == 401


class TestAccountState:
    async def test_a_deactivated_accounts_passkey_is_refused(
        self, client, passkey_actor
    ):
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, passkey_actor.actor.user_id)
            assert user is not None
            user.active = False
            await db.commit()

        response = await login_with_passkey(client, passkey_actor)
        assert response.status_code == 401

    async def test_a_temporarily_locked_account_still_logs_in_and_is_cleared(
        self, client, passkey_actor
    ):
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, passkey_actor.actor.user_id)
            assert user is not None
            user.locked_until = Clock.utcnow().replace(year=2999)
            user.failed_login_count = 4
            await db.commit()

        response = await login_with_passkey(client, passkey_actor)
        assert response.status_code == 200

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, passkey_actor.actor.user_id)
            assert user is not None
            assert user.failed_login_count == 0
            assert user.locked_until is None

    async def test_a_permanently_locked_account_is_refused(self, client, passkey_actor):
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, passkey_actor.actor.user_id)
            assert user is not None
            user.locked_permanently_at = Clock.utcnow()
            await db.commit()

        response = await login_with_passkey(client, passkey_actor)
        assert response.status_code == 401


class TestThrottling:
    async def test_a_failed_assertion_does_not_touch_the_account_counter(
        self, client, passkey_actor
    ):
        options_response = await passkey_options(client, passkey_actor.actor.username)
        garbage_credential = {
            "id": "not-a-real-credential-id",
            "rawId": "not-a-real-credential-id",
            "type": "public-key",
            "response": {
                "clientDataJSON": "",
                "authenticatorData": "",
                "signature": "",
                "userHandle": None,
            },
            "clientExtensionResults": {},
        }
        response = await passkey_login(
            client, options_response.json()["login_token"], garbage_credential
        )
        assert response.status_code == 401

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, passkey_actor.actor.user_id)
            assert user is not None
            assert user.failed_login_count == 0


class TestMalformedCredential:
    """§5.6 step 2: `credential["id"]` missing or not a string is refused
    before any database lookup happens."""

    async def test_a_missing_credential_id_is_refused(self, client, passkey_actor):
        options_response = await passkey_options(client, passkey_actor.actor.username)
        response = await passkey_login(
            client, options_response.json()["login_token"], {"response": {}}
        )
        assert response.status_code == 401

    async def test_a_non_string_credential_id_is_refused(self, client, passkey_actor):
        options_response = await passkey_options(client, passkey_actor.actor.username)
        response = await passkey_login(
            client,
            options_response.json()["login_token"],
            {"id": 12345, "response": {}},
        )
        assert response.status_code == 401


class TestTransportsAreFiltered:
    """The transports array rides beside the attestation rather than inside
    it, so nothing verifies it and a client may send anything. Left unfiltered
    it reaches `AuthenticatorTransport(...)` in `authentication_options`,
    where an unknown string is a ValueError — and since anyone may ask for a
    username's options, that is an unauthenticated 500 any account holder
    could arm."""

    async def test_an_unknown_transport_is_not_stored(
        self, client, roleless, password, authenticator
    ):
        device = authenticator()
        options = (
            await client.post(
                "/users/me/passkeys/options",
                headers=roleless.headers,
                json={"current_password": password},
            )
        ).json()
        credential = device.register(options["options"])
        credential["response"]["transports"] = ["internal", "bogus-transport"]

        created = await client.post(
            "/users/me/passkeys",
            headers=roleless.headers,
            json={
                "registration_token": options["registration_token"],
                "label": "Poisoned",
                "credential": credential,
            },
        )
        assert created.status_code == 200, created.text

        async with DatabaseService.session() as db:
            stored = await PasskeyCredential.list_for_user(db, roleless.user_id)
        assert stored[0].transports == ["internal"]

    async def test_a_poisoned_row_does_not_break_the_options_route(
        self, client, roleless
    ):
        """The same filter on the read side, for a row written before it
        existed — or by a build that did not have it."""
        async with DatabaseService.session() as db:
            db.add(
                PasskeyCredential(
                    user_id=roleless.user_id,
                    credential_id="legacy-row",
                    public_key="irrelevant",
                    transports=["bogus-transport"],
                    device_type="multi_device",
                    backed_up=True,
                    label="Legacy",
                )
            )
            await db.commit()

        response = await client.post(
            "/token/passkey/options", json={"username": roleless.username}
        )
        assert response.status_code == 200, response.text


class TestDisabledDeployment:
    async def test_both_routes_404_and_capabilities_reports_false(
        self, client, monkeypatch
    ):

        monkeypatch.setenv("WEBAUTHN_RP_ID", "")
        monkeypatch.setenv("WEBAUTHN_ALLOWED_ORIGINS", "")
        ConfigService.reset()

        capabilities = await client.get("/auth/capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json() == {"passkeys": False}

        options_response = await passkey_options(client, None)
        assert options_response.status_code == 404

        login_response = await passkey_login(client, "irrelevant", {})
        assert login_response.status_code == 404
