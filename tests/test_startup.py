"""Application startup, and the wiring in main."""

import logging
import secrets

import pytest
from cryptography.fernet import Fernet

import main
from persistence.mfa_credential import MfaCredential
from persistence.user import User
from services.auth.mfa.secret_box import MfaSecretBox
from services.auth.roles import Roles
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService


async def test_root_is_anonymous(client):
    response = await client.get("/")
    assert response.status_code == 200


async def test_root_takes_no_credential(client, admin):
    """It used to declare a token dependency it never checked."""
    assert (await client.get("/", headers=admin.headers)).status_code == 200


async def test_openapi_documents_both_document_shapes(client):
    schema = (await client.get("/openapi.json")).json()
    public = schema["paths"]["/public/{username}/resume/{document_name}"]["get"]
    private = schema["paths"]["/documents/resume/{document_name}"]["get"]

    def ref(route):
        return route["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ]

    assert ref(public) != ref(private)
    assert "Resume" in ref(public)
    assert "ResumePrivate" in ref(private)


async def test_public_schema_has_no_private_fields(client):
    schema = (await client.get("/openapi.json")).json()
    contact = schema["components"]["schemas"]["ContactPublic"]["properties"]
    assert set(contact) == {"locations", "links"}


async def test_mcp_app_is_mounted(client):
    """Unauthenticated, so it must not simply 404."""
    response = await client.get("/resume/mcp")
    assert response.status_code != 404


class TestLifespan:
    async def test_runs_migrations_and_claims_the_database(self, monkeypatch, password):
        """The container path: nothing but environment variables."""
        username = f"lifespan-{secrets.token_hex(4)}"
        monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", username)
        monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", password)

        async with main.application._lifespan(main.app):
            async with DatabaseService.session() as db:
                user = await User.get_user_by_username(db, username)
            assert user is not None
            assert user.roles == [Roles.ADMIN.value]

    async def test_migrations_are_idempotent(self):
        async with main.application._lifespan(main.app):
            pass
        async with main.application._lifespan(main.app):
            pass

    async def test_migrations_do_not_disable_application_logging(self):
        """Alembic calls logging.fileConfig, which switches off every existing
        logger unless told otherwise. Startup migrates before it does anything
        else, so the default would silence the app's own output — including the
        warning that there is no admin."""
        async with main.application._lifespan(main.app):
            pass
        assert main.Application.LOG.disabled is False
        assert main.Application.LOG.isEnabledFor(logging.WARNING)


class TestServiceProviders:
    def test_config_service_reads_settings(self):
        from services.config.config_service import ConfigService

        assert ConfigService.get_with_deps().settings.auth_algorithm
        assert ConfigService.get_without_deps().settings.auth_algorithm

    def test_database_echo_defaults_off(self):
        """Echoed SQL carries bind parameters, including password hashes."""
        from services.config.config_service import ConfigServiceModel

        assert ConfigServiceModel().database_echo is False

    def test_client_html_path_defaults_unset(self, monkeypatch):
        """Unset by default, so GET /client is not registered and nothing
        about a deployment that does not want this client changes.

        Built with no env file and the variable removed, because that is the
        only way to observe a *default*. A bare ConfigServiceModel() reads the
        developer's own .env — which is how this test failed the moment
        someone set CLIENT_HTML_PATH locally to try the /client route — and
        conftest now pins the variable to empty for the rest of the suite, so
        it would not see None here either.
        """
        from services.config.config_service import ConfigServiceModel

        monkeypatch.delenv("CLIENT_HTML_PATH", raising=False)
        assert (
            ConfigServiceModel(_env_file=None).client_html_path is None
            or ConfigServiceModel().client_html_path == ""
        )

    def test_client_html_path_reads_the_environment(self, monkeypatch):
        from services.config.config_service import ConfigServiceModel

        monkeypatch.setenv("CLIENT_HTML_PATH", "/srv/client/index.html")
        assert ConfigServiceModel().client_html_path == "/srv/client/index.html"


class TestVerifyMfaKey:
    """A wrong key is otherwise silent until an enrolled user tries to log
    in, and it locks all of them out at once."""

    @staticmethod
    async def _seal_a_credential(secret: str) -> None:
        async with DatabaseService.session() as db:
            user = User(username="verify-mfa-key-owner", password=None, roles=[])
            db.add(user)
            await db.flush()
            box = MfaSecretBox.from_settings(ConfigService.get_without_deps().settings)
            db.add(
                MfaCredential(
                    user_id=user.id,
                    kind="totp",
                    label="Authenticator app",
                    secret=box.seal(secret),
                )
            )
            await db.commit()

    async def test_silent_with_no_sealed_rows(self):
        tasks = main.StartupTasks(ConfigService.get_without_deps())
        await tasks.verify_mfa_key()  # must not raise

    async def test_silent_with_a_matching_key(self):
        await self._seal_a_credential("a-totp-secret")
        tasks = main.StartupTasks(ConfigService.get_without_deps())
        await tasks.verify_mfa_key()  # must not raise

    async def test_raises_with_a_mismatched_key(self, monkeypatch):
        await self._seal_a_credential("a-totp-secret")
        monkeypatch.setenv("MFA_ENCRYPTION_KEYS", Fernet.generate_key().decode())
        ConfigService.reset()
        tasks = main.StartupTasks(ConfigService.get_without_deps())
        with pytest.raises(RuntimeError, match="MFA_ENCRYPTION_KEYS"):
            await tasks.verify_mfa_key()
