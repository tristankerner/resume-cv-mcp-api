"""The two-step /token flow: POST /token returns a challenge when the
account is enrolled, and POST /token/mfa redeems it.
"""

import jwt

from persistence.mfa_credential import MfaCredential
from persistence.user import User
from services.auth.mfa.challenge import MfaChallengeContext, MfaChallengeToken
from services.auth.mfa.methods.backup_codes import BackupCodesMethod
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService


async def start_login(client, username: str, password: str):
    return await client.post(
        "/token", data={"username": username, "password": password}
    )


class TestUnenrolledLoginIsUnchanged:
    async def test_a_user_with_no_mfa_still_gets_a_token_in_one_step(
        self, client, admin
    ):
        response = await start_login(client, admin.username, admin.password)
        assert response.status_code == 200
        body = response.json()
        assert "access_token" in body
        assert "mfa_required" not in body


class TestFirstStep:
    async def test_an_enrolled_user_gets_a_challenge_not_a_token(
        self, client, enrolled
    ):
        response = await start_login(
            client, enrolled.actor.username, enrolled.actor.password
        )
        assert response.status_code == 200
        body = response.json()
        assert body["mfa_required"] is True
        assert "access_token" not in body
        assert body["methods"] == ["totp"]
        assert body["expires_in"] > 0
        assert body["mfa_token"]

    async def test_a_wrong_password_at_step_one_is_still_401(self, client, enrolled):
        response = await start_login(client, enrolled.actor.username, "wrong")
        assert response.status_code == 401


class TestSecondStep:
    async def test_a_valid_totp_code_redeems_the_challenge(
        self, client, enrolled, totp_code
    ):
        challenge = await start_login(
            client, enrolled.actor.username, enrolled.actor.password
        )
        mfa_token = challenge.json()["mfa_token"]

        response = await client.post(
            "/token/mfa",
            data={"mfa_token": mfa_token, "code": totp_code(enrolled.secret)},
        )
        assert response.status_code == 200
        assert "access_token" in response.json()

    async def test_a_valid_backup_code_redeems_the_challenge(self, client, enrolled):
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, enrolled.actor.user_id)
            assert user is not None
            method = BackupCodesMethod(db, ConfigService.get_without_deps().settings)
            result = await method.begin_enrollment(user, "Backup codes")
            assert result.codes is not None
            await db.commit()

        challenge = await start_login(
            client, enrolled.actor.username, enrolled.actor.password
        )
        mfa_token = challenge.json()["mfa_token"]

        response = await client.post(
            "/token/mfa", data={"mfa_token": mfa_token, "code": result.codes[0]}
        )
        assert response.status_code == 200
        assert "access_token" in response.json()

    async def test_a_wrong_code_is_401_and_counts_against_the_account(
        self, client, enrolled
    ):
        challenge = await start_login(
            client, enrolled.actor.username, enrolled.actor.password
        )
        mfa_token = challenge.json()["mfa_token"]

        response = await client.post(
            "/token/mfa", data={"mfa_token": mfa_token, "code": "000000"}
        )
        assert response.status_code == 401

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, enrolled.actor.user_id)
            assert user is not None
            assert user.failed_login_count == 1

    async def test_enough_wrong_codes_lock_the_account(
        self, client, enrolled, monkeypatch
    ):
        monkeypatch.setenv("AUTH_LOCKOUT_MAX_ATTEMPTS", "3")
        monkeypatch.setenv("AUTH_LOCKOUT_BASE_MINUTES", "1")
        monkeypatch.setenv("AUTH_IP_MAX_FAILURES", "10000")
        ConfigService.reset()

        challenge = await start_login(
            client, enrolled.actor.username, enrolled.actor.password
        )
        mfa_token = challenge.json()["mfa_token"]

        response = None
        for _ in range(3):
            response = await client.post(
                "/token/mfa", data={"mfa_token": mfa_token, "code": "000000"}
            )
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) > 0

    async def test_an_expired_challenge_is_401(self, client, enrolled):
        settings = ConfigService.get_without_deps().settings
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, enrolled.actor.user_id)
            assert user is not None
            token, _ = MfaChallengeToken.mint(settings, user, MfaChallengeContext.TOKEN)

        payload = jwt.decode(
            token,
            settings.auth_secret_key.get_secret_value(),
            algorithms=[settings.auth_algorithm],
        )
        payload["exp"] = payload["iat"] - 1
        expired = jwt.encode(
            payload,
            settings.auth_secret_key.get_secret_value(),
            algorithm=settings.auth_algorithm,
        )

        response = await client.post(
            "/token/mfa", data={"mfa_token": expired, "code": "000000"}
        )
        assert response.status_code == 401

    async def test_a_docs_challenge_is_refused_at_slash_token_slash_mfa(
        self, client, enrolled
    ):
        settings = ConfigService.get_without_deps().settings
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, enrolled.actor.user_id)
            assert user is not None
            token, _ = MfaChallengeToken.mint(settings, user, MfaChallengeContext.DOCS)

        response = await client.post(
            "/token/mfa", data={"mfa_token": token, "code": "000000"}
        )
        assert response.status_code == 401

    async def test_a_challenge_is_refused_after_the_password_changes(
        self, client, enrolled, totp_code
    ):
        challenge = await start_login(
            client, enrolled.actor.username, enrolled.actor.password
        )
        mfa_token = challenge.json()["mfa_token"]

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, enrolled.actor.user_id)
            assert user is not None
            user.password = "a-different-hash"
            await db.commit()

        response = await client.post(
            "/token/mfa",
            data={"mfa_token": mfa_token, "code": totp_code(enrolled.secret)},
        )
        assert response.status_code == 401

    async def test_a_deactivated_users_challenge_is_refused(
        self, client, enrolled, totp_code
    ):
        challenge = await start_login(
            client, enrolled.actor.username, enrolled.actor.password
        )
        mfa_token = challenge.json()["mfa_token"]

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, enrolled.actor.user_id)
            assert user is not None
            user.active = False
            await db.commit()

        response = await client.post(
            "/token/mfa",
            data={"mfa_token": mfa_token, "code": totp_code(enrolled.secret)},
        )
        assert response.status_code == 401

    async def test_a_correct_code_clears_the_failure_history(
        self, client, enrolled, totp_code
    ):
        # One wrong code first, to have something to clear.
        challenge = await start_login(
            client, enrolled.actor.username, enrolled.actor.password
        )
        await client.post(
            "/token/mfa",
            data={"mfa_token": challenge.json()["mfa_token"], "code": "000000"},
        )

        challenge = await start_login(
            client, enrolled.actor.username, enrolled.actor.password
        )
        response = await client.post(
            "/token/mfa",
            data={
                "mfa_token": challenge.json()["mfa_token"],
                "code": totp_code(enrolled.secret),
            },
        )
        assert response.status_code == 200

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, enrolled.actor.user_id)
            assert user is not None
            assert user.failed_login_count == 0


class TestReplayGuardAcrossChallenges:
    async def test_a_totp_code_cannot_be_reused_across_two_challenges(
        self, client, enrolled, totp_code
    ):
        code = totp_code(enrolled.secret)

        first = await start_login(
            client, enrolled.actor.username, enrolled.actor.password
        )
        first_response = await client.post(
            "/token/mfa", data={"mfa_token": first.json()["mfa_token"], "code": code}
        )
        assert first_response.status_code == 200

        second = await start_login(
            client, enrolled.actor.username, enrolled.actor.password
        )
        second_response = await client.post(
            "/token/mfa", data={"mfa_token": second.json()["mfa_token"], "code": code}
        )
        assert second_response.status_code == 401


class TestCredentialRowState:
    async def test_a_successful_login_advances_last_used_step(
        self, client, enrolled, totp_code
    ):
        # Enrolling already advances the step once, by design (activation is
        # itself a verified code) — so this asserts the login advances it
        # further, not that it starts unset.
        step_after_enrollment = (await self._credentials(enrolled.actor.user_id))[
            0
        ].last_used_step
        assert step_after_enrollment is not None

        challenge = await start_login(
            client, enrolled.actor.username, enrolled.actor.password
        )
        await client.post(
            "/token/mfa",
            data={
                "mfa_token": challenge.json()["mfa_token"],
                "code": totp_code(enrolled.secret),
            },
        )

        step_after_login = (await self._credentials(enrolled.actor.user_id))[
            0
        ].last_used_step
        assert step_after_login is not None
        assert step_after_login > step_after_enrollment

    @staticmethod
    async def _credentials(user_id: int) -> list[MfaCredential]:
        async with DatabaseService.session() as db:
            return await MfaCredential.list_for_user(db, user_id)
