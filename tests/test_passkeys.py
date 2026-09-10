"""The passkeys package: settings validation now, the relying party, the
ceremony wrapper, the challenge tokens and the sign-count claim as later
steps land. Mirrors tests/test_mfa.py's class-per-concern layout.
"""

import pytest
from cryptography.fernet import Fernet

from services.config.config_service import ConfigService


def settings():
    return ConfigService.get_without_deps().settings


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
