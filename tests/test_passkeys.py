"""The passkeys package: settings validation, persistence, the relying party,
the ceremony wrapper, the challenge tokens and the sign-count claim. Mirrors
tests/test_mfa.py's class-per-concern layout.
"""

import secrets

import jwt
import pytest
import webauthn
from cryptography.fernet import Fernet
from fastapi import HTTPException

from persistence.base import Base64Url, Clock
from persistence.passkey_credential import PasskeyCredential
from persistence.user import User
from services.auth.auth_service import AuthService
from services.auth.passkeys.ceremony import PasskeyCeremony
from services.auth.passkeys.challenge import (
    PasskeyAuthenticationChallenge,
    PasskeyContext,
    PasskeyRegistrationChallenge,
)
from services.auth.passkeys.relying_party import RelyingParty
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService
from tests.support.webauthn_authenticator import SoftwareAuthenticator


def settings():
    return ConfigService.get_without_deps().settings


async def make_user(username: str) -> User:
    async with DatabaseService.session() as db:
        user = User(username=username, password="irrelevant-hash", roles=[])
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


async def make_credential(
    user_id: int, credential_id: str = "cred-1", **overrides
) -> PasskeyCredential:
    async with DatabaseService.session() as db:
        credential = PasskeyCredential(
            user_id=user_id,
            credential_id=credential_id,
            public_key="pubkey",
            device_type="multi_device",
            backed_up=True,
            label="Test authenticator",
            **overrides,
        )
        db.add(credential)
        await db.commit()
        await db.refresh(credential)
        return credential


@pytest.fixture
def reconfigure(monkeypatch):
    """Set WEBAUTHN_* settings, and drop the memoised copy afterwards.

    Same shape as test_lockout.py's `reconfigure`: conftest's `fresh_settings`
    clears the cache around the test, but a fixture that only sets environment
    variables would be silently ignored if pytest ordered it after something
    that already built and cached settings.

    `None` clears a setting by writing an empty string, never by deleting the
    variable — conftest's module docstring spells out why: `ConfigServiceModel`
    reads `.env`, so a deleted variable falls through to whatever the developer
    running the suite happens to have configured. Deleting made these tests
    pass on a clean checkout and fail on any machine with WEBAUTHN_RP_ID in
    `.env`, which is the exact class of environment-dependent result the
    pinning in conftest exists to prevent.
    """

    def apply(**values):
        for key, value in values.items():
            monkeypatch.setenv(key, "" if value is None else str(value))
        ConfigService.reset()

    return apply


class TestSoftwareAuthenticatorSanity:
    """The load-bearing check for the whole plan: until this passes, nothing
    downstream can. Options come from the real `PasskeyCeremony`; the
    response comes from `SoftwareAuthenticator`; verification calls
    `webauthn.verify_registration_response` directly rather than through
    `PasskeyCeremony.verify_registration`, so a bug in our own wrapper cannot
    hide a bug in the authenticator double, or vice versa.
    """

    def test_a_registration_response_it_produces_verifies(self):
        relying_party = RelyingParty(settings())
        ceremony = PasskeyCeremony(relying_party)
        user = User(id=1, username="sanity-check", roles=[])

        options, challenge = ceremony.registration_options(user, b"handle", [])

        authenticator = SoftwareAuthenticator(
            rp_id=relying_party.rp_id, origin=relying_party.origins[0]
        )
        response = authenticator.register(options)

        verified = webauthn.verify_registration_response(
            credential=response,
            expected_challenge=challenge,
            expected_rp_id=relying_party.rp_id,
            expected_origin=relying_party.origins,
            require_user_verification=True,
        )
        assert verified.credential_id == authenticator.credential_id
        assert verified.credential_device_type.value == "multi_device"
        assert verified.credential_backed_up is True

    def test_an_authentication_response_it_produces_verifies(self):
        relying_party = RelyingParty(settings())
        ceremony = PasskeyCeremony(relying_party)
        user = User(id=1, username="sanity-check-auth", roles=[])
        user_handle = b"handle"

        registration_options, reg_challenge = ceremony.registration_options(
            user, user_handle, []
        )
        authenticator = SoftwareAuthenticator(
            rp_id=relying_party.rp_id, origin=relying_party.origins[0]
        )
        registration_response = authenticator.register(registration_options)
        registered = webauthn.verify_registration_response(
            credential=registration_response,
            expected_challenge=reg_challenge,
            expected_rp_id=relying_party.rp_id,
            expected_origin=relying_party.origins,
            require_user_verification=True,
        )

        auth_options, auth_challenge = ceremony.authentication_options([])
        assertion = authenticator.authenticate(auth_options, user_handle=user_handle)

        verified = webauthn.verify_authentication_response(
            credential=assertion,
            expected_challenge=auth_challenge,
            expected_rp_id=relying_party.rp_id,
            expected_origin=relying_party.origins,
            credential_public_key=registered.credential_public_key,
            credential_current_sign_count=registered.sign_count,
            require_user_verification=True,
        )
        assert verified.credential_id == authenticator.credential_id
        assert verified.new_sign_count == 0


class TestWebauthnSettings:
    """Every branch of `_validate_webauthn_origins` and the surrounding
    parsing — settings validation runs at load, so a mistake here is a
    startup failure rather than a runtime one."""

    def test_disabled_by_default(self, reconfigure):
        reconfigure(WEBAUTHN_RP_ID=None, WEBAUTHN_ALLOWED_ORIGINS=None)
        assert settings().passkeys_enabled is False

    def test_origins_without_an_rp_id_is_rejected(self, reconfigure):
        with pytest.raises(ValueError, match="WEBAUTHN_RP_ID"):
            reconfigure(
                WEBAUTHN_RP_ID=None, WEBAUTHN_ALLOWED_ORIGINS="https://example.com"
            )
            settings()

    def test_an_rp_id_without_origins_is_rejected(self, reconfigure):
        with pytest.raises(ValueError, match="WEBAUTHN_ALLOWED_ORIGINS"):
            reconfigure(WEBAUTHN_RP_ID="example.com", WEBAUTHN_ALLOWED_ORIGINS=None)
            settings()

    def test_a_non_origin_string_is_rejected(self, reconfigure):
        with pytest.raises(ValueError, match="is not an origin"):
            reconfigure(
                WEBAUTHN_RP_ID="example.com", WEBAUTHN_ALLOWED_ORIGINS="not-a-url"
            )
            settings()

    def test_a_cross_domain_origin_is_rejected(self, reconfigure):
        with pytest.raises(ValueError, match="is not the RP ID"):
            reconfigure(
                WEBAUTHN_RP_ID="example.com",
                WEBAUTHN_ALLOWED_ORIGINS="https://evil.example.org",
            )
            settings()

    def test_a_subdomain_origin_is_accepted(self, reconfigure):
        reconfigure(
            WEBAUTHN_RP_ID="example.com",
            WEBAUTHN_ALLOWED_ORIGINS="https://app.example.com",
        )
        assert settings().passkeys_enabled is True

    def test_http_in_production_is_rejected(self, reconfigure):
        with pytest.raises(ValueError, match="is not https"):
            reconfigure(
                ENVIRONMENT="production",
                WEBAUTHN_RP_ID="example.com",
                WEBAUTHN_ALLOWED_ORIGINS="http://example.com",
                PUBLIC_BASE_URL="https://example.com",
                MFA_ENCRYPTION_KEYS=Fernet.generate_key().decode(),
            )
            settings()

    def test_http_in_development_is_accepted(self, reconfigure):
        reconfigure(
            ENVIRONMENT="development",
            WEBAUTHN_RP_ID="example.com",
            WEBAUTHN_ALLOWED_ORIGINS="http://example.com",
        )
        assert settings().passkeys_enabled is True

    def test_a_trailing_slash_is_normalised_away(self, reconfigure):
        reconfigure(
            WEBAUTHN_RP_ID="example.com",
            WEBAUTHN_ALLOWED_ORIGINS="https://example.com/",
        )
        assert settings().webauthn_allowed_origins == {"https://example.com"}

    def test_an_empty_rp_id_is_treated_as_unset(self, reconfigure):
        """pydantic-settings hands back "" for a variable set and then
        cleared, not None — the same case a test that wants the feature off
        mid-run relies on."""
        reconfigure(WEBAUTHN_RP_ID="", WEBAUTHN_ALLOWED_ORIGINS=None)
        assert settings().passkeys_enabled is False


class TestRelyingParty:
    def test_enabled_reflects_the_settings(self, reconfigure):
        reconfigure(WEBAUTHN_RP_ID=None, WEBAUTHN_ALLOWED_ORIGINS=None)
        assert RelyingParty(settings()).enabled is False

        reconfigure(
            WEBAUTHN_RP_ID="example.com", WEBAUTHN_ALLOWED_ORIGINS="https://example.com"
        )
        assert RelyingParty(settings()).enabled is True

    def test_require_enabled_raises_when_off(self, reconfigure):
        reconfigure(WEBAUTHN_RP_ID=None, WEBAUTHN_ALLOWED_ORIGINS=None)
        with pytest.raises(HTTPException) as excinfo:
            RelyingParty(settings()).require_enabled()
        assert excinfo.value.status_code == 404

    def test_rp_id_raises_when_off(self, reconfigure):
        reconfigure(WEBAUTHN_RP_ID=None, WEBAUTHN_ALLOWED_ORIGINS=None)
        with pytest.raises(HTTPException):
            _ = RelyingParty(settings()).rp_id

    def test_origins_are_sorted(self, reconfigure):
        reconfigure(
            WEBAUTHN_RP_ID="example.com",
            WEBAUTHN_ALLOWED_ORIGINS="https://z.example.com,https://a.example.com",
        )
        assert RelyingParty(settings()).origins == [
            "https://a.example.com",
            "https://z.example.com",
        ]

    def test_serves_origin(self, reconfigure):
        reconfigure(
            WEBAUTHN_RP_ID="example.com",
            WEBAUTHN_ALLOWED_ORIGINS="https://example.com",
        )
        relying_party = RelyingParty(settings())
        assert relying_party.serves_origin("https://example.com") is True
        assert relying_party.serves_origin("https://other.example.com") is False

    def test_serves_origin_is_false_when_disabled(self, reconfigure):
        reconfigure(WEBAUTHN_RP_ID=None, WEBAUTHN_ALLOWED_ORIGINS=None)
        assert RelyingParty(settings()).serves_origin("https://example.com") is False


class TestPasskeyCeremony:
    def test_registration_options_demand_resident_key_and_user_verification(self):
        ceremony = PasskeyCeremony(RelyingParty(settings()))
        user = User(id=1, username="ceremony-options", roles=[])
        options, _challenge = ceremony.registration_options(user, b"handle", [])
        assert options["authenticatorSelection"]["residentKey"] == "required"
        assert options["authenticatorSelection"]["userVerification"] == "required"

    async def test_exclude_credentials_lists_what_the_user_holds(self):
        user_row = await make_user("ceremony-exclude")
        # A real base64url id, not an arbitrary label: `registration_options`
        # round-trips it through `Base64Url.decode` on the way in and
        # py_webauthn's own base64url encoder on the way out, and only bytes
        # actually produced by `Base64Url.encode` survive that intact.
        raw_id = secrets.token_bytes(16)
        credential = await make_credential(
            user_row.id, Base64Url.encode(raw_id), transports=["internal"]
        )

        ceremony = PasskeyCeremony(RelyingParty(settings()))
        user = User(id=user_row.id, username=user_row.username, roles=[])
        options, _challenge = ceremony.registration_options(
            user, b"handle", [credential]
        )
        excluded_ids = {entry["id"] for entry in options["excludeCredentials"]}
        assert credential.credential_id in excluded_ids

    def test_a_registration_for_the_wrong_origin_does_not_verify(self):
        relying_party = RelyingParty(settings())
        ceremony = PasskeyCeremony(relying_party)
        user = User(id=1, username="ceremony-wrong-origin", roles=[])
        options, challenge = ceremony.registration_options(user, b"handle", [])

        authenticator = SoftwareAuthenticator(
            rp_id=relying_party.rp_id, origin="http://not-the-configured-origin"
        )
        response = authenticator.register(options)

        with pytest.raises(PasskeyCeremony.REGISTRATION_FAILURE):
            ceremony.verify_registration(response, challenge)

    def test_a_registration_for_the_wrong_rp_id_does_not_verify(self):
        relying_party = RelyingParty(settings())
        ceremony = PasskeyCeremony(relying_party)
        user = User(id=1, username="ceremony-wrong-rp", roles=[])
        options, challenge = ceremony.registration_options(user, b"handle", [])

        authenticator = SoftwareAuthenticator(
            rp_id="not-the-configured-rp-id", origin=relying_party.origins[0]
        )
        response = authenticator.register(options)

        with pytest.raises(PasskeyCeremony.REGISTRATION_FAILURE):
            ceremony.verify_registration(response, challenge)

    def test_a_well_formed_registration_verifies_through_the_wrapper(self):
        relying_party = RelyingParty(settings())
        ceremony = PasskeyCeremony(relying_party)
        user = User(id=1, username="ceremony-good", roles=[])
        options, challenge = ceremony.registration_options(user, b"handle", [])

        authenticator = SoftwareAuthenticator(
            rp_id=relying_party.rp_id, origin=relying_party.origins[0]
        )
        response = authenticator.register(options)
        verified = ceremony.verify_registration(response, challenge)
        assert verified.credential_id == authenticator.credential_id

    def test_authentication_options_omit_allow_credentials_when_empty(self):
        ceremony = PasskeyCeremony(RelyingParty(settings()))
        options, _challenge = ceremony.authentication_options([])
        assert not options.get("allowCredentials")

    def test_a_well_formed_authentication_verifies_through_the_wrapper(self):
        relying_party = RelyingParty(settings())
        ceremony = PasskeyCeremony(relying_party)
        user = User(id=1, username="ceremony-auth-good", roles=[])
        user_handle = b"handle"
        reg_options, reg_challenge = ceremony.registration_options(
            user, user_handle, []
        )
        authenticator = SoftwareAuthenticator(
            rp_id=relying_party.rp_id, origin=relying_party.origins[0]
        )
        reg_response = authenticator.register(reg_options)
        verified_registration = ceremony.verify_registration(
            reg_response, reg_challenge
        )

        stored = PasskeyCredential(
            user_id=1,
            credential_id=Base64Url.encode(verified_registration.credential_id),
            public_key=Base64Url.encode(verified_registration.credential_public_key),
            sign_count=verified_registration.sign_count,
            transports=[],
            device_type=verified_registration.credential_device_type.value,
            backed_up=verified_registration.credential_backed_up,
            label="Test",
        )

        auth_options, auth_challenge = ceremony.authentication_options([stored])
        assertion = authenticator.authenticate(auth_options, user_handle=user_handle)
        verified = ceremony.verify_authentication(assertion, auth_challenge, stored)
        assert verified.new_sign_count == 0


class TestPasskeyChallenge:
    """`PasskeyRegistrationChallenge` and `PasskeyAuthenticationChallenge`,
    independent of any particular caller — same approach as
    tests/test_mfa.py's TestMfaChallengeToken."""

    async def test_registration_challenge_round_trips(self):
        user = await make_user("challenge-reg-roundtrip")
        token, expires_in = PasskeyRegistrationChallenge.mint(
            settings(), user, b"a-challenge"
        )
        assert expires_in > 0
        assert (
            PasskeyRegistrationChallenge.redeem(settings(), token, user)
            == b"a-challenge"
        )

    async def test_registration_challenge_is_refused_after_the_password_changes(self):
        user = await make_user("challenge-reg-pwb")
        token, _expires_in = PasskeyRegistrationChallenge.mint(
            settings(), user, b"a-challenge"
        )
        user.password = "a-different-hash"
        assert PasskeyRegistrationChallenge.redeem(settings(), token, user) is None

    async def test_registration_challenge_is_refused_for_another_account(self):
        user = await make_user("challenge-reg-account-a")
        other = await make_user("challenge-reg-account-b")
        token, _expires_in = PasskeyRegistrationChallenge.mint(
            settings(), user, b"a-challenge"
        )
        assert PasskeyRegistrationChallenge.redeem(settings(), token, other) is None

    def test_authentication_challenge_round_trips(self):
        token, expires_in = PasskeyAuthenticationChallenge.mint(
            settings(), b"a-challenge", 7, PasskeyContext.TOKEN
        )
        assert expires_in > 0
        assert PasskeyAuthenticationChallenge.redeem(
            settings(), token, PasskeyContext.TOKEN
        ) == (b"a-challenge", 7)

    def test_authentication_challenge_carries_no_subject_for_the_usernameless_flow(
        self,
    ):
        token, _expires_in = PasskeyAuthenticationChallenge.mint(
            settings(), b"a-challenge", None, PasskeyContext.TOKEN
        )
        assert PasskeyAuthenticationChallenge.redeem(
            settings(), token, PasskeyContext.TOKEN
        ) == (b"a-challenge", None)

    def test_a_token_context_token_is_refused_for_docs(self):
        token, _expires_in = PasskeyAuthenticationChallenge.mint(
            settings(), b"a-challenge", None, PasskeyContext.TOKEN
        )
        assert (
            PasskeyAuthenticationChallenge.redeem(
                settings(), token, PasskeyContext.DOCS
            )
            is None
        )

    def test_an_oauth_token_bound_to_one_client_id_is_refused_for_another(self):
        token, _expires_in = PasskeyAuthenticationChallenge.mint(
            settings(),
            b"a-challenge",
            None,
            PasskeyContext.OAUTH,
            binding="client-a",
        )
        assert (
            PasskeyAuthenticationChallenge.redeem(
                settings(), token, PasskeyContext.OAUTH, binding="client-b"
            )
            is None
        )
        assert PasskeyAuthenticationChallenge.redeem(
            settings(), token, PasskeyContext.OAUTH, binding="client-a"
        ) == (b"a-challenge", None)

    def test_an_expired_authentication_challenge_is_refused(self):
        token, _expires_in = PasskeyAuthenticationChallenge.mint(
            settings(), b"a-challenge", None, PasskeyContext.TOKEN
        )
        decoded = jwt.decode(
            token,
            settings().auth_secret_key.get_secret_value(),
            algorithms=[settings().auth_algorithm],
        )
        decoded["exp"] = decoded["iat"] - 1
        expired = jwt.encode(
            decoded,
            settings().auth_secret_key.get_secret_value(),
            algorithm=settings().auth_algorithm,
        )
        assert (
            PasskeyAuthenticationChallenge.redeem(
                settings(), expired, PasskeyContext.TOKEN
            )
            is None
        )

    async def test_a_passkey_auth_token_does_not_authenticate_as_a_bearer_token(self):
        """The `token_use` guard in `AuthService._authenticate_jwt` is the
        reason `SignedToken` exists at all — assert it explicitly rather than
        trusting the generic coverage in tests/test_auth.py."""
        token, _expires_in = PasskeyAuthenticationChallenge.mint(
            settings(), b"a-challenge", None, PasskeyContext.TOKEN
        )
        async with DatabaseService.session() as db:
            service = AuthService(db, token, ConfigService.get_without_deps())
            assert await service.authenticate() is None


class TestWebauthnUserHandle:
    """`User.ensure_webauthn_handle` and `get_by_webauthn_handle`."""

    async def test_a_fresh_user_has_no_handle(self):
        user = await make_user("handle-fresh")
        assert user.webauthn_user_handle is None

    async def test_ensure_assigns_a_handle_once(self):
        user = await make_user("handle-assign")
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, user.id)
            assert user is not None
            first = User.ensure_webauthn_handle(user)
            await db.commit()
        assert first

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, user.id)
            assert user is not None
            second = User.ensure_webauthn_handle(user)
        assert second == first

    async def test_get_by_webauthn_handle_finds_the_right_user(self):
        user = await make_user("handle-lookup")
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, user.id)
            assert user is not None
            handle = User.ensure_webauthn_handle(user)
            await db.commit()

        async with DatabaseService.session() as db:
            found = await User.get_by_webauthn_handle(db, handle)
            assert found is not None
            assert found.id == user.id

    async def test_an_unknown_handle_finds_nobody(self):
        async with DatabaseService.session() as db:
            assert await User.get_by_webauthn_handle(db, "no-such-handle") is None


class TestPasskeyCredentialPersistence:
    async def test_list_for_user_is_ordered_by_creation(self):
        user = await make_user("cred-list")
        first = await make_credential(user.id, "cred-list-1")
        second = await make_credential(user.id, "cred-list-2")

        async with DatabaseService.session() as db:
            credentials = await PasskeyCredential.list_for_user(db, user.id)
        assert [c.id for c in credentials] == [first.id, second.id]

    async def test_get_for_user_is_scoped_to_the_owner(self):
        owner = await make_user("cred-owner")
        stranger = await make_user("cred-stranger")
        credential = await make_credential(owner.id, "cred-owner-1")

        async with DatabaseService.session() as db:
            assert (
                await PasskeyCredential.get_for_user(db, owner.id, credential.id)
                is not None
            )
            assert (
                await PasskeyCredential.get_for_user(db, stranger.id, credential.id)
                is None
            )

    async def test_get_by_credential_id_is_unscoped(self):
        user = await make_user("cred-lookup")
        credential = await make_credential(user.id, "cred-lookup-1")

        async with DatabaseService.session() as db:
            found = await PasskeyCredential.get_by_credential_id(db, "cred-lookup-1")
            assert found is not None
            assert found.id == credential.id
            assert (
                await PasskeyCredential.get_by_credential_id(db, "no-such-id") is None
            )

    async def test_delete_all_for_user_removes_every_row(self):
        user = await make_user("cred-delete")
        await make_credential(user.id, "cred-delete-1")
        await make_credential(user.id, "cred-delete-2")

        async with DatabaseService.session() as db:
            removed = await PasskeyCredential.delete_all_for_user(db, user.id)
            await db.commit()
        assert removed == 2

        async with DatabaseService.session() as db:
            assert await PasskeyCredential.list_for_user(db, user.id) == []


class TestClaimSignCount:
    async def test_zero_stays_zero_and_still_updates_last_used_at(self):
        user = await make_user("sign-count-zero")
        credential = await make_credential(user.id, "sign-count-zero-1")
        assert credential.last_used_at is None

        now = Clock.utcnow()
        async with DatabaseService.session() as db:
            credential = await PasskeyCredential.get_for_user(
                db, user.id, credential.id
            )
            assert credential is not None
            won = await PasskeyCredential.claim_sign_count(credential, db, 0, now)
            await db.commit()
        assert won
        assert credential.sign_count == 0
        assert credential.last_used_at == now

    async def test_an_advance_wins(self):
        user = await make_user("sign-count-advance")
        credential = await make_credential(user.id, "sign-count-advance-1")

        now = Clock.utcnow()
        async with DatabaseService.session() as db:
            credential = await PasskeyCredential.get_for_user(
                db, user.id, credential.id
            )
            assert credential is not None
            won = await PasskeyCredential.claim_sign_count(credential, db, 5, now)
            await db.commit()
        assert won
        assert credential.sign_count == 5

    async def test_a_repeat_of_the_same_non_zero_count_loses(self):
        user = await make_user("sign-count-replay")
        credential = await make_credential(user.id, "sign-count-replay-1")

        now = Clock.utcnow()
        async with DatabaseService.session() as db:
            credential = await PasskeyCredential.get_for_user(
                db, user.id, credential.id
            )
            assert credential is not None
            assert await PasskeyCredential.claim_sign_count(credential, db, 5, now)
            await db.commit()

        async with DatabaseService.session() as db:
            credential = await PasskeyCredential.get_for_user(
                db, user.id, credential.id
            )
            assert credential is not None
            replayed = await PasskeyCredential.claim_sign_count(
                credential, db, 5, Clock.utcnow()
            )
            await db.commit()
        assert not replayed
        assert credential.sign_count == 5
