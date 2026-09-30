"""Serving GET /.well-known/assetlinks.json for the Android client, and the
settings validation behind it.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from main import AssetLinksRoute
from services.config.config_service import ConfigService


def settings():
    return ConfigService.get_without_deps().settings


@pytest.fixture
def reconfigure(monkeypatch):
    """Same shape as test_passkeys.py's fixture of the same name: set
    ANDROID_* settings and drop the memoised copy, using an empty string
    rather than deleting the variable so a developer's own .env can never
    leak into the result.
    """

    def apply(**values):
        for key, value in values.items():
            monkeypatch.setenv(key, "" if value is None else str(value))
        ConfigService.reset()

    return apply


def _app_serving(
    package_name: str | None, fingerprints: frozenset[str]
) -> tuple[FastAPI, bool]:
    """A bare app with the route registered, rather than a reload of `main`.

    Registration happens once at import against the process's settings, so
    the shared `client` fixture can never exercise both branches. Driving the
    function directly is what makes the unconfigured case testable at all.
    """
    app = FastAPI()
    registered = AssetLinksRoute.register(app, package_name, fingerprints)
    return app, registered


class TestConfigured:
    def test_serves_the_expected_shape(self):
        app, registered = _app_serving(
            "com.tristankerner.resumeapi.client", frozenset({"AA:BB:CC"})
        )
        assert registered

        response = TestClient(app).get("/.well-known/assetlinks.json")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.json() == [
            {
                "relation": ["delegate_permission/common.get_login_creds"],
                "target": {
                    "namespace": "android_app",
                    "package_name": "com.tristankerner.resumeapi.client",
                    "sha256_cert_fingerprints": ["AA:BB:CC"],
                },
            }
        ]

    def test_lists_every_fingerprint_sorted(self):
        """Sorted, so a deployment with two trusted certs (a key rotation in
        progress) gets a stable body across processes."""
        app, _ = _app_serving(
            "com.tristankerner.resumeapi.client",
            frozenset({"BB:BB:BB", "AA:AA:AA"}),
        )

        response = TestClient(app).get("/.well-known/assetlinks.json")
        fingerprints = response.json()[0]["target"]["sha256_cert_fingerprints"]
        assert fingerprints == ["AA:AA:AA", "BB:BB:BB"]

    def test_stays_out_of_the_openapi_schema(self):
        app, _ = _app_serving("com.example.app", frozenset({"AA:BB:CC"}))
        assert "/.well-known/assetlinks.json" not in app.openapi()["paths"]

    def test_takes_no_credential(self):
        app, _ = _app_serving("com.example.app", frozenset({"AA:BB:CC"}))
        response = TestClient(app).get("/.well-known/assetlinks.json")
        assert response.status_code == 200


class TestNotConfigured:
    def test_neither_setting_registers_nothing(self):
        app, registered = _app_serving(None, frozenset())
        assert not registered
        assert TestClient(app).get("/.well-known/assetlinks.json").status_code == 404

    def test_package_name_without_fingerprints_registers_nothing(self):
        app, registered = _app_serving("com.example.app", frozenset())
        assert not registered
        assert TestClient(app).get("/.well-known/assetlinks.json").status_code == 404


class TestAndroidAssetLinksSettings:
    """Every branch of `_validate_android_asset_links` and the surrounding
    parsing — settings validation runs at load, so a mistake here is a
    startup failure rather than a 404 somebody has to chase down."""

    def test_disabled_by_default(self, reconfigure):
        reconfigure(ANDROID_PACKAGE_NAME=None, ANDROID_SHA256_CERT_FINGERPRINTS=None)
        assert settings().android_asset_links_enabled is False

    def test_fingerprints_without_a_package_name_is_rejected(self, reconfigure):
        with pytest.raises(ValueError, match="ANDROID_PACKAGE_NAME"):
            reconfigure(
                ANDROID_PACKAGE_NAME=None,
                ANDROID_SHA256_CERT_FINGERPRINTS="AA:BB:CC",
            )
            settings()

    def test_a_package_name_without_fingerprints_is_rejected(self, reconfigure):
        with pytest.raises(ValueError, match="ANDROID_SHA256_CERT_FINGERPRINTS"):
            reconfigure(
                ANDROID_PACKAGE_NAME="com.example.app",
                ANDROID_SHA256_CERT_FINGERPRINTS=None,
            )
            settings()

    def test_both_set_is_accepted(self, reconfigure):
        reconfigure(
            ANDROID_PACKAGE_NAME="com.example.app",
            ANDROID_SHA256_CERT_FINGERPRINTS="AA:BB:CC",
        )
        assert settings().android_asset_links_enabled is True

    def test_fingerprints_are_uppercased_and_split_on_commas(self, reconfigure):
        reconfigure(
            ANDROID_PACKAGE_NAME="com.example.app",
            ANDROID_SHA256_CERT_FINGERPRINTS="aa:bb:cc, dd:ee:ff",
        )
        assert settings().android_sha256_cert_fingerprints == {
            "AA:BB:CC",
            "DD:EE:FF",
        }

    def test_an_empty_package_name_is_treated_as_unset(self, reconfigure):
        """pydantic-settings hands back "" for a variable set and then
        cleared, not None — the same case a test that wants the feature off
        mid-run relies on."""
        reconfigure(ANDROID_PACKAGE_NAME="", ANDROID_SHA256_CERT_FINGERPRINTS=None)
        assert settings().android_asset_links_enabled is False
