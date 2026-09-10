"""MfaSecretBox: sealing TOTP secrets at rest, and the settings that
configure it. Exercised directly against the class, following
test_lockout.py's approach of testing policy objects without a database.
"""

from types import SimpleNamespace
from typing import cast

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from persistence.mfa_credential import MfaCredential
from services.auth.mfa.secret_box import MfaSecretBox
from services.config.config_service import ConfigServiceModel
from services.database.database_service import DatabaseService


def settings_with_keys(*raw_keys: str) -> ConfigServiceModel:
    """A minimal stand-in for ConfigServiceModel — MfaSecretBox.from_settings
    only ever reads `mfa_encryption_keys` off it, so a real settings object
    would only add noise (a full env, a database URL) unrelated to what
    these tests are about."""
    return cast(
        ConfigServiceModel,
        SimpleNamespace(mfa_encryption_keys=[SecretStr(key) for key in raw_keys]),
    )


class TestRoundTrip:
    def test_open_of_seal_returns_the_plaintext(self):
        box = MfaSecretBox.from_settings(
            settings_with_keys(Fernet.generate_key().decode())
        )
        assert box.open(box.seal("a-totp-secret")) == "a-totp-secret"

    def test_sealed_output_carries_the_prefix(self):
        box = MfaSecretBox.from_settings(
            settings_with_keys(Fernet.generate_key().decode())
        )
        assert box.seal("a-totp-secret").startswith(MfaSecretBox.PREFIX)

    def test_two_seals_of_the_same_plaintext_differ_and_both_open(self):
        box = MfaSecretBox.from_settings(
            settings_with_keys(Fernet.generate_key().decode())
        )
        first = box.seal("a-totp-secret")
        second = box.seal("a-totp-secret")
        assert first != second
        assert box.open(first) == "a-totp-secret"
        assert box.open(second) == "a-totp-secret"

    def test_tampered_ciphertext_returns_none_rather_than_raising(self):
        box = MfaSecretBox.from_settings(
            settings_with_keys(Fernet.generate_key().decode())
        )
        sealed = box.seal("a-totp-secret")
        tampered = sealed[:-1] + ("a" if sealed[-1] != "a" else "b")
        result = box.open(tampered)
        assert result is None

    def test_a_key_not_in_the_configured_list_returns_none(self):
        sealed = MfaSecretBox.from_settings(
            settings_with_keys(Fernet.generate_key().decode())
        ).seal("a-totp-secret")
        other_box = MfaSecretBox.from_settings(
            settings_with_keys(Fernet.generate_key().decode())
        )
        assert other_box.open(sealed) is None


class TestRotation:
    def test_a_value_sealed_under_an_old_key_still_opens_once_a_new_one_is_added(self):
        key_a = Fernet.generate_key().decode()
        key_b = Fernet.generate_key().decode()
        sealed_under_b = MfaSecretBox.from_settings(settings_with_keys(key_b)).seal(
            "a-totp-secret"
        )

        rotated_box = MfaSecretBox.from_settings(settings_with_keys(key_a, key_b))
        assert rotated_box.open(sealed_under_b) == "a-totp-secret"

    def test_reseal_moves_a_value_onto_the_primary_key(self):
        key_a = Fernet.generate_key().decode()
        key_b = Fernet.generate_key().decode()
        sealed_under_b = MfaSecretBox.from_settings(settings_with_keys(key_b)).seal(
            "a-totp-secret"
        )

        rotated_box = MfaSecretBox.from_settings(settings_with_keys(key_a, key_b))
        resealed = rotated_box.reseal(sealed_under_b)

        # Opens under key A alone now — the whole point of the reseal.
        key_a_only_box = MfaSecretBox.from_settings(settings_with_keys(key_a))
        assert key_a_only_box.open(resealed) == "a-totp-secret"

    def test_reseal_of_an_unopenable_value_returns_it_unchanged(self):
        """A failure here must not destroy a secret — the caller cannot
        distinguish "resealed" from "left alone" and must not need to."""
        stranger_box = MfaSecretBox.from_settings(
            settings_with_keys(Fernet.generate_key().decode())
        )
        sealed_elsewhere = stranger_box.seal("a-totp-secret")

        box = MfaSecretBox.from_settings(
            settings_with_keys(Fernet.generate_key().decode())
        )
        assert box.reseal(sealed_elsewhere) == sealed_elsewhere


class TestNullBox:
    def test_unconfigured_box_is_the_identity(self):
        box = MfaSecretBox.from_settings(settings_with_keys())
        assert box.seal("a-totp-secret") == "a-totp-secret"
        assert box.open("a-totp-secret") == "a-totp-secret"
        assert MfaSecretBox.is_sealed("a-totp-secret") is False

    def test_an_unprefixed_value_still_opens_once_a_key_is_configured(self):
        """The "added a key later" path: a row written before MFA_ENCRYPTION_KEYS
        existed is not locked out by adding one."""
        box = MfaSecretBox.from_settings(
            settings_with_keys(Fernet.generate_key().decode())
        )
        assert box.open("a-legacy-plaintext-secret") == "a-legacy-plaintext-secret"


class TestSettingsValidation:
    def test_rejects_a_malformed_encryption_key(self, monkeypatch):
        monkeypatch.setenv("MFA_ENCRYPTION_KEYS", "not-a-valid-fernet-key")
        with pytest.raises(ValueError, match="MFA_ENCRYPTION_KEYS"):
            ConfigServiceModel()

    def test_accepts_a_well_formed_key(self, monkeypatch):
        monkeypatch.setenv("MFA_ENCRYPTION_KEYS", Fernet.generate_key().decode())
        settings = ConfigServiceModel()
        assert len(settings.mfa_encryption_keys) == 1

    def test_accepts_several_comma_separated_keys(self, monkeypatch):
        keys = [Fernet.generate_key().decode() for _ in range(2)]
        monkeypatch.setenv("MFA_ENCRYPTION_KEYS", ",".join(keys))
        settings = ConfigServiceModel()
        assert len(settings.mfa_encryption_keys) == 2

    def test_refuses_to_build_in_production_with_no_key(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("MFA_ENCRYPTION_KEYS", "")
        # Off, not the pinned plain-http origin from conftest: that origin
        # fails production's separate https-only WebAuthn check, which is not
        # what this test is about.
        monkeypatch.setenv("WEBAUTHN_RP_ID", "")
        monkeypatch.setenv("WEBAUTHN_ALLOWED_ORIGINS", "")
        with pytest.raises(ValueError, match="MFA_ENCRYPTION_KEYS"):
            ConfigServiceModel()

    def test_production_is_fine_with_a_key_configured(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("MFA_ENCRYPTION_KEYS", Fernet.generate_key().decode())
        monkeypatch.setenv("WEBAUTHN_RP_ID", "")
        monkeypatch.setenv("WEBAUTHN_ALLOWED_ORIGINS", "")
        assert ConfigServiceModel().is_production is True


class TestPlaintextNeverReachesTheColumn:
    """The test that would actually catch someone removing the seal call in
    TotpMethod.begin_enrollment — everything else here tests the box in
    isolation, not that the login path actually uses it."""

    async def test_the_base32_seed_is_not_stored_in_the_clear(
        self, client, roleless, password
    ):
        response = await client.post(
            "/users/me/mfa/totp",
            headers=roleless.headers,
            json={"label": "Phone", "current_password": password},
        )
        assert response.status_code == 200
        secret = response.json()["secret"]
        credential_id = response.json()["credential_id"]

        async with DatabaseService.session() as db:
            credential = await MfaCredential.get_for_user(
                db, roleless.user_id, credential_id
            )
            assert credential is not None
            assert credential.secret is not None
            assert secret not in credential.secret
            assert MfaSecretBox.is_sealed(credential.secret)
