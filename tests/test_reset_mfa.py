"""The break-glass MFA reset: `python -m reset_mfa`."""

import pyotp

from persistence.mfa_credential import MfaCredential
from persistence.user import User
from reset_mfa import ResetMfaCommand
from services.auth.mfa.methods.totp import TotpMethod
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService


class TestResetMfaCommand:
    async def test_it_removes_every_credential(self, enrolled, capsys):
        assert await ResetMfaCommand().run([enrolled.actor.username]) == 0
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

        assert await ResetMfaCommand().run([enrolled.actor.username]) == 0
        assert "Removed 2 MFA method(s)" in capsys.readouterr().out

    async def test_it_reports_an_account_with_nothing_to_remove(self, admin, capsys):
        assert await ResetMfaCommand().run([admin.username]) == 0
        assert "has no MFA methods" in capsys.readouterr().out

    async def test_it_reports_an_unknown_user(self, capsys):
        assert await ResetMfaCommand().run(["nobody-at-all"]) == 1
        assert "No user named" in capsys.readouterr().err

    async def test_it_never_decrypts_a_secret(self, enrolled, monkeypatch):
        """The break-glass property: this tool must work even when
        MFA_ENCRYPTION_KEYS is wrong, so it must never call MfaSecretBox.open."""
        from services.auth.mfa import secret_box

        def refuses_to_open(self, stored):
            raise AssertionError("reset_mfa must never open a sealed secret")

        monkeypatch.setattr(secret_box.MfaSecretBox, "open", refuses_to_open)

        assert await ResetMfaCommand().run([enrolled.actor.username]) == 0

    async def test_list_reports_nothing_by_default(self, admin, capsys):
        assert await ResetMfaCommand().run(["--list"]) == 0
        assert "No accounts have an MFA method." in capsys.readouterr().out

    async def test_list_shows_an_enrolled_account_and_changes_nothing(
        self, enrolled, capsys
    ):
        assert await ResetMfaCommand().run(["--list"]) == 0
        output = capsys.readouterr().out
        assert enrolled.actor.username in output
        assert "totp" in output

        async with DatabaseService.session() as db:
            remaining = await MfaCredential.list_for_user(db, enrolled.actor.user_id)
        assert len(remaining) == 1

    async def test_it_needs_a_username_or_list(self, capsys):
        assert await ResetMfaCommand().run([]) == 1
        assert "Give a username" in capsys.readouterr().err
