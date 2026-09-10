"""The passkeys package: settings validation, persistence, the relying party,
the ceremony wrapper, the challenge tokens and the sign-count claim. Mirrors
tests/test_mfa.py's class-per-concern layout.
"""

import pytest
from cryptography.fernet import Fernet

from persistence.base import Clock
from persistence.passkey_credential import PasskeyCredential
from persistence.user import User
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService


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
    """

    def apply(**values):
        for key, value in values.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, str(value))
        ConfigService.reset()

    return apply


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
