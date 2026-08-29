"""The break-glass password reset: `python -m reset_password`."""

import pytest

from persistence.user import User
from reset_password import ResetPasswordCommand
from services.auth.auth_service import AuthService
from services.database.database_service import DatabaseService


async def read_user(user_id: int) -> User:
    async with DatabaseService.session() as db:
        user = await User.get_user_by_id(db, user_id)
        assert user is not None
        return user


class TestResetPasswordCommand:
    async def test_it_sets_a_new_password(self, admin, monkeypatch, capsys):
        import reset_password

        answers = iter(["NewPassw0rd!", "NewPassw0rd!"])
        monkeypatch.setattr(reset_password.getpass, "getpass", lambda *a: next(answers))

        assert await ResetPasswordCommand().run([admin.username]) == 0
        assert "Password reset" in capsys.readouterr().out

        user = await read_user(admin.user_id)
        assert AuthService.verify_password("NewPassw0rd!", user.password)

    async def test_it_reports_an_unknown_user(self, capsys):
        assert await ResetPasswordCommand().run(["nobody-at-all"]) == 1
        assert "No user named" in capsys.readouterr().err

    async def test_it_reprompts_on_mismatched_retype(self, admin, monkeypatch, capsys):
        import reset_password

        answers = iter(["NewPassw0rd!", "Different1!", "NewPassw0rd!", "NewPassw0rd!"])
        monkeypatch.setattr(reset_password.getpass, "getpass", lambda *a: next(answers))

        assert await ResetPasswordCommand().run([admin.username]) == 0
        assert "did not match" in capsys.readouterr().err

        user = await read_user(admin.user_id)
        assert AuthService.verify_password("NewPassw0rd!", user.password)

    async def test_it_reprompts_on_a_weak_password(self, admin, monkeypatch, capsys):
        import reset_password

        answers = iter(["weak", "weak", "NewPassw0rd!", "NewPassw0rd!"])
        monkeypatch.setattr(reset_password.getpass, "getpass", lambda *a: next(answers))

        assert await ResetPasswordCommand().run([admin.username]) == 0

        user = await read_user(admin.user_id)
        assert AuthService.verify_password("NewPassw0rd!", user.password)

    async def test_it_needs_a_username(self):
        with pytest.raises(SystemExit):
            await ResetPasswordCommand().run([])

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
        import reset_password

        answers = iter(["NewPassw0rd!", "NewPassw0rd!"])
        monkeypatch.setattr(reset_password.getpass, "getpass", lambda *a: next(answers))

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

        assert await ResetPasswordCommand().run([admin.username]) == 1
        assert "No user named" in capsys.readouterr().err
        assert calls["n"] == 2
