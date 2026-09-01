"""The consolidated break-glass CLI: `python -m admin_cli <subcommand>`.

Replaces the tests for the four scripts admin_cli.py absorbed: the
`TestUnlockCommand` class that used to live in test_lockout.py,
test_reset_password.py, test_reset_mfa.py, and the `TestCli` class that used
to live in test_bootstrap.py — one class per subcommand of one script now.
"""

import secrets
from datetime import timedelta

import pyotp
import pytest

import admin_cli
from admin_cli import AdminCli
from persistence.auth_failure import AuthFailure
from persistence.base import utcnow
from persistence.mfa_credential import MfaCredential
from persistence.user import User
from services.auth.auth_service import AuthService
from services.auth.mfa.methods.totp import TotpMethod
from services.auth.roles import Roles
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService


async def read_user(user_id: int) -> User:
    async with DatabaseService.session() as db:
        user = await User.get_user_by_id(db, user_id)
        assert user is not None
        return user


async def role_of(username: str) -> list[str] | None:
    async with DatabaseService.session() as db:
        user = await User.get_user_by_username(db, username)
        return None if user is None else list(user.roles)


class TestArgParsing:
    async def test_no_subcommand_exits_nonzero_with_usage_on_stderr(self, capsys):
        with pytest.raises(SystemExit) as caught:
            await AdminCli().run([])
        assert caught.value.code != 0
        assert "usage:" in capsys.readouterr().err

    async def test_an_unknown_subcommand_exits_nonzero(self, capsys):
        with pytest.raises(SystemExit):
            await AdminCli().run(["not-a-subcommand"])
        assert "usage:" in capsys.readouterr().err


class TestUnlockCommand:
    async def test_it_clears_a_permanent_lock(self, admin, capsys):
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, admin.user_id)
            assert user is not None
            user.locked_permanently_at = utcnow()
            await db.commit()

        assert await AdminCli().run(["unlock", admin.username]) == 0
        assert "Unlocked" in capsys.readouterr().out

        user = await read_user(admin.user_id)
        assert user.locked_permanently_at is None

    async def test_it_reports_an_unknown_user(self, capsys):
        assert await AdminCli().run(["unlock", "nobody-at-all"]) == 1
        assert "No user named" in capsys.readouterr().err

    async def test_it_clears_a_banned_address(self, capsys):
        async with DatabaseService.session() as db:
            db.add(
                AuthFailure(
                    address="203.0.113.7",
                    last_failure_at=utcnow(),
                    banned_until=utcnow() + timedelta(hours=1),
                )
            )
            await db.commit()

        assert await AdminCli().run(["unlock", "--list"]) == 0
        assert "203.0.113.7" in capsys.readouterr().out

        assert await AdminCli().run(["unlock", "--address", "203.0.113.7"]) == 0

        async with DatabaseService.session() as db:
            assert await AuthFailure.get_by_address(db, "203.0.113.7") is None

    async def test_it_lists_what_is_locked(self, admin, capsys):
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, admin.user_id)
            assert user is not None
            user.locked_until = utcnow() + timedelta(hours=1)
            await db.commit()

        assert await AdminCli().run(["unlock", "--list"]) == 0
        assert admin.username in capsys.readouterr().out

    async def test_it_needs_something_to_do(self, capsys):
        assert await AdminCli().run(["unlock"]) == 1

    async def test_it_says_so_when_nothing_is_locked(self, capsys):
        assert await AdminCli().run(["unlock", "--list"]) == 0
        assert "Nothing is locked" in capsys.readouterr().out

    async def test_it_lists_a_permanent_lock(self, admin, capsys):
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, admin.user_id)
            assert user is not None
            user.locked_permanently_at = utcnow()
            await db.commit()

        assert await AdminCli().run(["unlock", "--list"]) == 0
        assert "locked permanently" in capsys.readouterr().out

    async def test_unlocking_an_open_account_changes_nothing(self, admin, capsys):
        assert await AdminCli().run(["unlock", admin.username]) == 0
        assert "was not locked" in capsys.readouterr().out

    async def test_clearing_an_unknown_address_is_not_an_error(self, capsys):
        assert await AdminCli().run(["unlock", "--address", "203.0.113.99"]) == 0
        assert "No failures recorded" in capsys.readouterr().out


class TestResetPasswordCommand:
    async def test_it_sets_a_new_password(self, admin, monkeypatch, capsys):
        answers = iter(["NewPassw0rd!", "NewPassw0rd!"])
        monkeypatch.setattr(admin_cli.getpass, "getpass", lambda *a: next(answers))

        assert await AdminCli().run(["reset-password", admin.username]) == 0
        assert "Password reset" in capsys.readouterr().out

        user = await read_user(admin.user_id)
        assert AuthService.verify_password("NewPassw0rd!", user.password)

    async def test_it_reports_an_unknown_user(self, capsys):
        assert await AdminCli().run(["reset-password", "nobody-at-all"]) == 1
        assert "No user named" in capsys.readouterr().err

    async def test_it_reprompts_on_mismatched_retype(self, admin, monkeypatch, capsys):
        answers = iter(["NewPassw0rd!", "Different1!", "NewPassw0rd!", "NewPassw0rd!"])
        monkeypatch.setattr(admin_cli.getpass, "getpass", lambda *a: next(answers))

        assert await AdminCli().run(["reset-password", admin.username]) == 0
        assert "did not match" in capsys.readouterr().err

        user = await read_user(admin.user_id)
        assert AuthService.verify_password("NewPassw0rd!", user.password)

    async def test_it_reprompts_on_a_weak_password(self, admin, monkeypatch, capsys):
        answers = iter(["weak", "weak", "NewPassw0rd!", "NewPassw0rd!"])
        monkeypatch.setattr(admin_cli.getpass, "getpass", lambda *a: next(answers))

        assert await AdminCli().run(["reset-password", admin.username]) == 0

        user = await read_user(admin.user_id)
        assert AuthService.verify_password("NewPassw0rd!", user.password)

    async def test_it_needs_a_username(self):
        with pytest.raises(SystemExit):
            await AdminCli().run(["reset-password"])

    async def test_it_reports_a_user_that_vanished_during_the_prompt(
        self, admin, monkeypatch, capsys
    ):
        """The account exists when the tool starts and not when it writes.

        The session is deliberately not held open across `getpass` — a
        connection idle in a transaction for however long someone takes to
        type is one the server may close first — so the row is re-read
        afterwards, and by then it can be gone. Driven by making the second
        lookup miss rather than by deleting mid-prompt, because the prompt is
        synchronous and cannot await a delete from inside the running loop.
        """
        answers = iter(["NewPassw0rd!", "NewPassw0rd!"])
        monkeypatch.setattr(admin_cli.getpass, "getpass", lambda *a: next(answers))

        real_lookup = User.get_user_by_username
        calls = {"n": 0}

        async def vanishes_after_the_first_lookup(db, username):
            calls["n"] += 1
            if calls["n"] == 1:
                return await real_lookup(db, username)
            return None

        monkeypatch.setattr(
            User, "get_user_by_username", vanishes_after_the_first_lookup
        )

        assert await AdminCli().run(["reset-password", admin.username]) == 1
        assert "No user named" in capsys.readouterr().err
        assert calls["n"] == 2


class TestResetMfaCommand:
    async def test_it_removes_every_credential(self, enrolled, capsys):
        assert await AdminCli().run(["reset-mfa", enrolled.actor.username]) == 0
        assert "Removed 1 MFA method(s)" in capsys.readouterr().out

        async with DatabaseService.session() as db:
            remaining = await MfaCredential.list_for_user(db, enrolled.actor.user_id)
        assert remaining == []

    async def test_it_removes_more_than_one_credential(self, enrolled, capsys):
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, enrolled.actor.user_id)
            assert user is not None
            method = TotpMethod(db, ConfigService.get_without_deps().settings)
            result = await method.begin_enrollment(user, "Laptop")
            assert result.secret is not None
            await db.commit()
            credential = await MfaCredential.get_for_user(
                db, enrolled.actor.user_id, result.credential_id
            )
            assert credential is not None
            await method.complete_enrollment(
                credential, pyotp.TOTP(result.secret).now()
            )
            await db.commit()

        assert await AdminCli().run(["reset-mfa", enrolled.actor.username]) == 0
        assert "Removed 2 MFA method(s)" in capsys.readouterr().out

    async def test_it_reports_an_account_with_nothing_to_remove(self, admin, capsys):
        assert await AdminCli().run(["reset-mfa", admin.username]) == 0
        assert "has no MFA methods" in capsys.readouterr().out

    async def test_it_reports_an_unknown_user(self, capsys):
        assert await AdminCli().run(["reset-mfa", "nobody-at-all"]) == 1
        assert "No user named" in capsys.readouterr().err

    async def test_it_never_decrypts_a_secret(self, enrolled, monkeypatch):
        """The break-glass property: this tool must work even when
        MFA_ENCRYPTION_KEYS is wrong, so it must never call MfaSecretBox.open."""
        from services.auth.mfa import secret_box

        def refuses_to_open(self, stored):
            raise AssertionError("reset-mfa must never open a sealed secret")

        monkeypatch.setattr(secret_box.MfaSecretBox, "open", refuses_to_open)

        assert await AdminCli().run(["reset-mfa", enrolled.actor.username]) == 0

    async def test_list_reports_nothing_by_default(self, admin, capsys):
        assert await AdminCli().run(["reset-mfa", "--list"]) == 0
        assert "No accounts have an MFA method." in capsys.readouterr().out

    async def test_list_shows_an_enrolled_account_and_changes_nothing(
        self, enrolled, capsys
    ):
        assert await AdminCli().run(["reset-mfa", "--list"]) == 0
        output = capsys.readouterr().out
        assert enrolled.actor.username in output
        assert "totp" in output

        async with DatabaseService.session() as db:
            remaining = await MfaCredential.list_for_user(db, enrolled.actor.user_id)
        assert len(remaining) == 1

    async def test_it_needs_a_username_or_list(self, capsys):
        assert await AdminCli().run(["reset-mfa"]) == 1
        assert "Give a username" in capsys.readouterr().err


class TestBootstrapAdminCommand:
    @pytest.fixture
    def credentials(self, password) -> tuple[str, str]:
        return f"claimed-{secrets.token_hex(4)}", password

    async def test_uses_the_environment_without_prompting(
        self, monkeypatch, credentials
    ):
        username, password = credentials
        monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", username)
        monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", password)
        monkeypatch.setattr(
            "builtins.input", lambda *a: pytest.fail("should not prompt")
        )

        assert await AdminCli().run(["bootstrap-admin"]) == 0
        assert await role_of(username) == [Roles.ADMIN.value]

    async def test_prompts_when_nothing_is_configured(self, monkeypatch, credentials):
        username, password = credentials
        monkeypatch.setattr("builtins.input", lambda *a: username)
        monkeypatch.setattr(admin_cli.getpass, "getpass", lambda *a: password)

        assert await AdminCli().run(["bootstrap-admin"]) == 0
        assert await role_of(username) == [Roles.ADMIN.value]

    async def test_rejects_mismatched_retype(self, monkeypatch, credentials, capsys):
        username, password = credentials
        answers = iter([password, password + "different"])
        monkeypatch.setattr("builtins.input", lambda *a: username)
        monkeypatch.setattr(admin_cli.getpass, "getpass", lambda *a: next(answers))

        assert await AdminCli().run(["bootstrap-admin"]) == 1
        assert "do not match" in capsys.readouterr().err

    async def test_rejects_a_blank_username(self, monkeypatch, password, capsys):
        monkeypatch.setattr("builtins.input", lambda *a: "   ")
        monkeypatch.setattr(admin_cli.getpass, "getpass", lambda *a: password)

        assert await AdminCli().run(["bootstrap-admin"]) == 1
        assert "required" in capsys.readouterr().err

    async def test_does_not_prompt_when_already_claimed(
        self, admin, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            "builtins.input", lambda *a: pytest.fail("should not prompt")
        )
        assert await AdminCli().run(["bootstrap-admin"]) == 0
        assert "already exists" in capsys.readouterr().out

    async def test_reports_a_validation_failure(self, monkeypatch, credentials, capsys):
        username, _ = credentials
        monkeypatch.setattr("builtins.input", lambda *a: username)
        monkeypatch.setattr(admin_cli.getpass, "getpass", lambda *a: "weak")

        assert await AdminCli().run(["bootstrap-admin"]) == 1
        assert "at least 8 characters" in capsys.readouterr().err
