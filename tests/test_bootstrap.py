"""Claiming a database that has no admin: `AdminBootstrapper` itself, and the
startup hook that calls it. The CLI path (`python -m admin_cli
bootstrap-admin`) has its own tests in test_admin_cli.py."""

import logging
import secrets

import pytest

import main
from main import StartupTasks
from persistence.document import Document
from persistence.user import User
from services.auth.roles import Roles
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService
from services.user.bootstrap import AdminBootstrapper, BootstrapError, BootstrapOutcome


@pytest.fixture
def startup_logs():
    """Capture straight off the application logger.

    caplog attaches to root, and the uvicorn logger the app writes to does not
    reliably propagate there once uvicorn has configured it.
    """
    messages: list[str] = []

    class Sink(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    handler = Sink()
    previous_level = main.StartupTasks.LOG.level
    main.StartupTasks.LOG.addHandler(handler)
    main.StartupTasks.LOG.setLevel(logging.DEBUG)
    try:
        yield messages
    finally:
        main.StartupTasks.LOG.removeHandler(handler)
        main.StartupTasks.LOG.setLevel(previous_level)


@pytest.fixture
def credentials(password) -> tuple[str, str]:
    """A username unique to the test, so nothing collides with seeded users."""
    return f"claimed-{secrets.token_hex(4)}", password


async def run(username, password, email=None):
    async with DatabaseService.session() as db:
        return await AdminBootstrapper(db).ensure_admin(username, password, email)


async def bootstrap_admin_user() -> None:
    await StartupTasks(ConfigService.get_without_deps()).bootstrap_admin_user()


async def role_of(username: str) -> list[str] | None:
    async with DatabaseService.session() as db:
        user = await User.get_user_by_username(db, username)
        return None if user is None else list(user.roles)


class TestEnsureAdmin:
    async def test_creates_an_admin_on_an_empty_database(self, credentials):
        username, password = credentials
        outcome, created = await run(username, password)
        assert outcome is BootstrapOutcome.CREATED
        assert created == username
        assert await role_of(username) == [Roles.ADMIN.value]

    async def test_the_bootstrap_admin_is_seeded_with_documents(self, credentials):
        """The bootstrap admin is a new user like any other — see
        services/user/document_seeder.py."""
        username, password = credentials
        await run(username, password)
        async with DatabaseService.session() as db:
            user = await User.get_user_by_username(db, username)
            assert user is not None
            docs = await Document.list_latest(db, user.id)
        assert {doc.name for doc in docs} == {"resume", "metadata", "skill"}
        assert all(doc.public is False for doc in docs)

    async def test_created_admin_can_log_in(self, client, credentials):
        username, password = credentials
        await run(username, password)
        response = await client.post(
            "/token", data={"username": username, "password": password}
        )
        assert response.status_code == 200

    async def test_email_is_stored(self, credentials):
        username, password = credentials
        await run(username, password, "admin@example.invalid")
        async with DatabaseService.session() as db:
            user = await User.get_user_by_username(db, username)
        assert user is not None
        assert user.email == "admin@example.invalid"

    async def test_no_op_once_an_admin_exists(self, admin, credentials):
        username, password = credentials
        outcome, created = await run(username, password)
        assert outcome is BootstrapOutcome.ALREADY_CLAIMED
        assert created is None
        assert await role_of(username) is None

    async def test_running_twice_creates_one_admin(self, credentials):
        username, password = credentials
        assert (await run(username, password))[0] is BootstrapOutcome.CREATED
        assert (await run(username, password))[0] is BootstrapOutcome.ALREADY_CLAIMED

    @pytest.mark.parametrize(
        "username,supply_password", [(None, True), ("", True), ("   ", True)]
    )
    async def test_missing_username_is_not_configured(
        self, username, supply_password, password
    ):
        outcome, _ = await run(username, password if supply_password else None)
        assert outcome is BootstrapOutcome.NOT_CONFIGURED

    async def test_missing_password_is_not_configured(self, credentials):
        username, _ = credentials
        assert (await run(username, None))[0] is BootstrapOutcome.NOT_CONFIGURED
        assert (await run(username, ""))[0] is BootstrapOutcome.NOT_CONFIGURED

    async def test_nothing_configured_is_not_an_error(self):
        assert (await run(None, None))[0] is BootstrapOutcome.NOT_CONFIGURED

    async def test_weak_password_is_fatal(self, credentials):
        username, _ = credentials
        with pytest.raises(BootstrapError) as caught:
            await run(username, "weak")
        assert "at least 8 characters" in str(caught.value)

    async def test_error_does_not_echo_the_password(self, credentials):
        username, _ = credentials
        secret = "weak"
        with pytest.raises(BootstrapError) as caught:
            await run(username, secret)
        assert secret not in str(caught.value)

    async def test_no_admin_is_created_when_validation_fails(self, credentials):
        username, _ = credentials
        with pytest.raises(BootstrapError):
            await run(username, "weak")
        assert await role_of(username) is None

    async def test_existing_non_admin_name_is_fatal(self, roleless, password):
        """Promoting an account by setting an environment variable would be a
        surprising way to grant admin."""
        with pytest.raises(BootstrapError) as caught:
            await run(roleless.username, password)
        assert "already exists" in str(caught.value)


class TestStartupHook:
    async def test_creates_the_configured_admin(self, monkeypatch, credentials):
        username, password = credentials
        monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", username)
        monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", password)

        await bootstrap_admin_user()
        assert await role_of(username) == [Roles.ADMIN.value]

    async def test_the_startup_hook_seeds_documents_too(self, monkeypatch, credentials):
        username, password = credentials
        monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", username)
        monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", password)

        await bootstrap_admin_user()
        async with DatabaseService.session() as db:
            user = await User.get_user_by_username(db, username)
            assert user is not None
            docs = await Document.list_latest(db, user.id)
        assert {doc.name for doc in docs} == {"resume", "metadata", "skill"}

    async def test_warns_but_starts_when_unconfigured(self, startup_logs):
        await bootstrap_admin_user()
        assert any("No admin user exists" in line for line in startup_logs)

    async def test_warning_names_the_way_out(self, startup_logs):
        await bootstrap_admin_user()
        combined = " ".join(startup_logs)
        assert "BOOTSTRAP_ADMIN_USERNAME" in combined
        assert "admin_cli bootstrap-admin" in combined

    async def test_is_quiet_when_already_claimed(self, admin, startup_logs):
        await bootstrap_admin_user()
        assert not any("No admin user exists" in line for line in startup_logs)

    async def test_logs_the_created_admin(self, monkeypatch, credentials, startup_logs):
        username, password = credentials
        monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", username)
        monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", password)

        await bootstrap_admin_user()
        combined = " ".join(startup_logs)
        assert username in combined
        assert password not in combined

    async def test_misconfiguration_stops_startup(self, monkeypatch, credentials):
        username, _ = credentials
        monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", username)
        monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "weak")

        with pytest.raises(BootstrapError):
            await bootstrap_admin_user()
