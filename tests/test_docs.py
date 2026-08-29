"""The documentation endpoints, and the login in front of them in production."""

import pytest

from persistence.user import User
from services.config.config_service import ConfigServiceModel, Environment
from services.database.database_service import DatabaseService

DOC_PATHS = ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"]


@pytest.fixture
def production(monkeypatch):
    """Run the next request as the deployed service.

    Settings are memoised for the life of the process, but conftest's
    `fresh_settings` drops that cache around every test, so setting the
    variable here is enough to have it read on the next request.
    """
    monkeypatch.setenv("ENVIRONMENT", "production")


class TestEnvironmentSetting:
    def test_defaults_to_development(self, monkeypatch):
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        settings = ConfigServiceModel()
        assert settings.environment is Environment.DEVELOPMENT
        assert settings.is_production is False

    def test_production_is_recognised(self, production):
        assert ConfigServiceModel().is_production is True

    def test_case_and_whitespace_are_forgiven(self, monkeypatch):
        """Deployment platforms are not consistent about case, and being strict
        about it means a service that will not start."""
        monkeypatch.setenv("ENVIRONMENT", " Production ")
        assert ConfigServiceModel().is_production is True

    def test_an_unrecognised_value_is_rejected(self, monkeypatch):
        """Not silently development: a typo would then be an open deployment."""
        monkeypatch.setenv("ENVIRONMENT", "prod")
        with pytest.raises(ValueError):
            ConfigServiceModel()


class TestDevelopment:
    @pytest.mark.parametrize("path", DOC_PATHS)
    async def test_needs_no_credential(self, client, path):
        assert (await client.get(path)).status_code == 200

    async def test_swagger_ui_points_at_the_schema(self, client):
        assert "/openapi.json" in (await client.get("/docs")).text

    async def test_schema_is_the_real_one(self, client):
        """The route is ours now rather than FastAPI's, so it has to be checked
        that it still serves the application's own schema."""
        schema = (await client.get("/openapi.json")).json()
        assert "/token" in schema["paths"]

    @pytest.mark.parametrize("path", DOC_PATHS)
    async def test_the_docs_are_not_in_the_docs(self, client, path):
        assert path not in (await client.get("/openapi.json")).json()["paths"]


class TestProduction:
    @pytest.mark.parametrize("path", DOC_PATHS)
    async def test_refuses_an_anonymous_request(self, client, production, path):
        response = await client.get(path)
        assert response.status_code == 401

    async def test_asks_the_browser_for_a_password(self, client, production):
        """Without this header the browser shows an error page rather than a
        login prompt, and there is no way to supply credentials at all."""
        response = await client.get("/docs")
        assert response.headers["WWW-Authenticate"].startswith("Basic")

    @pytest.mark.parametrize("path", DOC_PATHS)
    async def test_serves_a_user_with_the_right_password(
        self, client, production, admin, path
    ):
        response = await client.get(path, auth=(admin.username, admin.password))
        assert response.status_code == 200

    async def test_any_account_will_do(self, client, production, roleless):
        """This gates who sees the route list, not what they may call: every
        route behind it enforces its own scopes."""
        response = await client.get(
            "/openapi.json", auth=(roleless.username, roleless.password)
        )
        assert response.status_code == 200

    async def test_rejects_the_wrong_password(self, client, production, admin):
        response = await client.get("/docs", auth=(admin.username, "not-the-password"))
        assert response.status_code == 401

    async def test_rejects_an_unknown_user(self, client, production, password):
        response = await client.get("/docs", auth=("nobody-at-all", password))
        assert response.status_code == 401

    async def test_rejects_a_bearer_token(self, client, production, admin):
        """A JWT is not a Basic credential. The docs are opened by a browser,
        which has nowhere to keep a token, so only the password path is here."""
        response = await client.get("/docs", headers=admin.headers)
        assert response.status_code == 401

    async def test_rejects_a_malformed_header(self, client, production):
        response = await client.get(
            "/docs", headers={"Authorization": "Basic not-base64"}
        )
        assert response.status_code == 401

    async def test_rejects_a_deactivated_user(
        self, client, production, password, hashed_password
    ):
        """The same rules as /token, because it is the same call: deactivating
        a user closes this door with the rest of them."""
        async with DatabaseService.session() as db:
            db.add(
                User(
                    username="deactivated",
                    password=hashed_password,
                    roles=[],
                    active=False,
                )
            )
            await db.commit()

        response = await client.get("/docs", auth=("deactivated", password))
        assert response.status_code == 401

    async def test_rejects_a_passwordless_account(self, client, production, password):
        """Service accounts are provisioned without one and cannot log in."""
        async with DatabaseService.session() as db:
            db.add(User(username="passwordless-docs", password=None, roles=[]))
            await db.commit()

        response = await client.get("/docs", auth=("passwordless-docs", password))
        assert response.status_code == 401
