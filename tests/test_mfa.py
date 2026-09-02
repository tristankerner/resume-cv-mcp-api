"""The MFA methods, the registry, and MfaVerifier — exercised directly
against real rows in a session, following test_lockout.py's approach of
testing the policy objects without going through HTTP.
"""

import time
from datetime import UTC, datetime
from typing import cast

import pyotp
import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.base import Clock
from persistence.mfa_credential import MfaCredential
from persistence.user import User
from services.auth.mfa.challenge import MfaChallengeContext, MfaChallengeToken
from services.auth.mfa.kinds import MfaMethodKind
from services.auth.mfa.methods.backup_codes import BackupCodesMethod
from services.auth.mfa.methods.totp import TotpMethod
from services.auth.mfa.registry import MfaMethodRegistry
from services.auth.mfa.verifier import MfaVerifier
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService


def settings():
    return ConfigService.get_without_deps().settings


def code_at(secret: str, now: datetime, counter_offset: int = 0) -> str:
    """A code for `now`, computed the same way an authenticator (and
    TotpMethod.verify) does: `now` is naive UTC by project convention, and
    must be tagged aware before pyotp sees it, or a host whose local
    timezone is not UTC would get a code for the wrong moment."""
    return pyotp.TOTP(secret).at(now.replace(tzinfo=UTC), counter_offset=counter_offset)


async def make_user(username: str) -> User:
    async with DatabaseService.session() as db:
        user = User(username=username, password="irrelevant-hash", roles=[])
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


class TestTotpMethod:
    async def test_enrollment_returns_a_secret_and_an_otpauth_uri(self):
        user = await make_user("totp-enroll")
        async with DatabaseService.session() as db:
            method = TotpMethod(db, settings())
            result = await method.begin_enrollment(user, "Phone")
            await db.commit()

        assert result.kind is MfaMethodKind.TOTP
        assert result.secret
        assert result.otpauth_uri is not None
        assert result.otpauth_uri.startswith("otpauth://totp/")

    async def test_verify_accepts_a_code_for_the_current_step(self):
        user = await make_user("totp-current")
        async with DatabaseService.session() as db:
            method = TotpMethod(db, settings())
            result = await method.begin_enrollment(user, "Phone")
            await db.commit()
            credential = await MfaCredential.get_for_user(
                db, user.id, result.credential_id
            )
            assert result.secret is not None
            assert credential is not None
            code = pyotp.TOTP(result.secret).now()
            assert await method.verify(credential, code, Clock.utcnow())

    async def test_verify_tolerates_spaces_in_the_entered_code(self):
        user = await make_user("totp-spaces")
        async with DatabaseService.session() as db:
            method = TotpMethod(db, settings())
            result = await method.begin_enrollment(user, "Phone")
            await db.commit()
            credential = await MfaCredential.get_for_user(
                db, user.id, result.credential_id
            )
            assert result.secret is not None
            assert credential is not None
            raw = pyotp.TOTP(result.secret).now()
            spaced = f"{raw[:3]} {raw[3:]}"
            assert await method.verify(credential, spaced, Clock.utcnow())

    async def test_verify_accepts_one_step_of_drift_either_side(self):
        user = await make_user("totp-drift")
        async with DatabaseService.session() as db:
            method = TotpMethod(db, settings())
            result = await method.begin_enrollment(user, "Phone")
            assert result.secret is not None
            await db.commit()

            now = Clock.utcnow()
            for counter_offset in (-1, 1):
                credential = await MfaCredential.get_for_user(
                    db, user.id, result.credential_id
                )
                assert credential is not None
                credential.last_used_step = None
                code = code_at(result.secret, now, counter_offset)
                assert await method.verify(credential, code, now)
                await db.commit()

    async def test_verify_rejects_two_steps_of_drift(self):
        user = await make_user("totp-too-much-drift")
        async with DatabaseService.session() as db:
            method = TotpMethod(db, settings())
            result = await method.begin_enrollment(user, "Phone")
            assert result.secret is not None
            await db.commit()

            now = Clock.utcnow()
            for counter_offset in (-2, 2):
                credential = await MfaCredential.get_for_user(
                    db, user.id, result.credential_id
                )
                assert credential is not None
                code = code_at(result.secret, now, counter_offset)
                assert not await method.verify(credential, code, now)

    async def test_a_code_cannot_be_replayed_even_within_its_own_step(self):
        user = await make_user("totp-replay")
        async with DatabaseService.session() as db:
            method = TotpMethod(db, settings())
            result = await method.begin_enrollment(user, "Phone")
            await db.commit()
            credential = await MfaCredential.get_for_user(
                db, user.id, result.credential_id
            )
            assert result.secret is not None
            assert credential is not None
            now = Clock.utcnow()
            code = pyotp.TOTP(result.secret).now()
            assert await method.verify(credential, code, now)
            await db.commit()

            credential = await MfaCredential.get_for_user(
                db, user.id, result.credential_id
            )
            assert credential is not None
            assert not await method.verify(credential, code, now)

    async def test_a_step_earlier_than_last_used_is_rejected(self):
        user = await make_user("totp-earlier-step")
        async with DatabaseService.session() as db:
            method = TotpMethod(db, settings())
            result = await method.begin_enrollment(user, "Phone")
            assert result.secret is not None
            await db.commit()

            now = Clock.utcnow()
            credential = await MfaCredential.get_for_user(
                db, user.id, result.credential_id
            )
            assert credential is not None
            later_code = code_at(result.secret, now, 1)
            assert await method.verify(credential, later_code, now)
            await db.commit()

            credential = await MfaCredential.get_for_user(
                db, user.id, result.credential_id
            )
            assert credential is not None
            earlier_code = code_at(result.secret, now, 0)
            assert not await method.verify(credential, earlier_code, now)

    async def test_correct_under_a_non_utc_process_timezone(self, monkeypatch):
        """pyotp treats a naive datetime as local time; Clock.utcnow() is naive UTC
        by project convention. CI runs in UTC, so this bug would not
        otherwise surface until deployment."""
        monkeypatch.setenv("TZ", "America/New_York")
        time.tzset()
        try:
            user = await make_user("totp-non-utc-tz")
            async with DatabaseService.session() as db:
                method = TotpMethod(db, settings())
                result = await method.begin_enrollment(user, "Phone")
                await db.commit()
                credential = await MfaCredential.get_for_user(
                    db, user.id, result.credential_id
                )
                assert result.secret is not None
                assert credential is not None
                code = pyotp.TOTP(result.secret).now()
                assert await method.verify(credential, code, Clock.utcnow())
        finally:
            monkeypatch.delenv("TZ", raising=False)
            time.tzset()

    async def test_complete_enrollment_activates_on_a_good_code(self):
        user = await make_user("totp-activate-good")
        async with DatabaseService.session() as db:
            method = TotpMethod(db, settings())
            result = await method.begin_enrollment(user, "Phone")
            await db.commit()
            credential = await MfaCredential.get_for_user(
                db, user.id, result.credential_id
            )
            assert result.secret is not None
            assert credential is not None
            code = pyotp.TOTP(result.secret).now()
            await method.complete_enrollment(credential, code)
            await db.commit()
            assert credential.is_active

    async def test_complete_enrollment_refuses_a_bad_code(self):
        user = await make_user("totp-activate-bad")
        async with DatabaseService.session() as db:
            method = TotpMethod(db, settings())
            result = await method.begin_enrollment(user, "Phone")
            await db.commit()
            credential = await MfaCredential.get_for_user(
                db, user.id, result.credential_id
            )
            assert credential is not None
            with pytest.raises(HTTPException) as excinfo:
                await method.complete_enrollment(credential, "000000")
            assert excinfo.value.status_code == 401
            assert not credential.is_active

    async def test_complete_enrollment_refuses_an_already_active_credential(self):
        user = await make_user("totp-activate-twice")
        async with DatabaseService.session() as db:
            method = TotpMethod(db, settings())
            result = await method.begin_enrollment(user, "Phone")
            await db.commit()
            credential = await MfaCredential.get_for_user(
                db, user.id, result.credential_id
            )
            assert result.secret is not None
            assert credential is not None
            code = pyotp.TOTP(result.secret).now()
            await method.complete_enrollment(credential, code)
            await db.commit()

            with pytest.raises(HTTPException) as excinfo:
                await method.complete_enrollment(credential, code)
            assert excinfo.value.status_code == 409


class TestBackupCodesMethod:
    async def test_a_generated_code_verifies_once_and_never_again(self):
        user = await make_user("backup-once")
        async with DatabaseService.session() as db:
            method = BackupCodesMethod(db, settings())
            result = await method.begin_enrollment(user, "Backup codes")
            await db.commit()
            credential = await MfaCredential.get_for_user(
                db, user.id, result.credential_id
            )
            assert result.codes is not None
            assert credential is not None
            code = result.codes[0]
            assert await method.verify(credential, code, Clock.utcnow())
            await db.commit()

            credential = await MfaCredential.get_for_user(
                db, user.id, result.credential_id
            )
            assert credential is not None
            assert not await method.verify(credential, code, Clock.utcnow())

    async def test_formatting_separators_are_tolerated(self):
        user = await make_user("backup-format")
        async with DatabaseService.session() as db:
            method = BackupCodesMethod(db, settings())
            result = await method.begin_enrollment(user, "Backup codes")
            await db.commit()
            credential = await MfaCredential.get_for_user(
                db, user.id, result.credential_id
            )
            assert result.codes is not None
            assert credential is not None
            code = result.codes[0]
            mangled = code.upper().replace("-", " ")
            assert await method.verify(credential, mangled, Clock.utcnow())

    async def test_remaining_counts_down(self):
        user = await make_user("backup-remaining")
        async with DatabaseService.session() as db:
            method = BackupCodesMethod(db, settings())
            result = await method.begin_enrollment(user, "Backup codes")
            await db.commit()
            credential = await MfaCredential.get_for_user(
                db, user.id, result.credential_id
            )
            assert result.codes is not None
            assert credential is not None
            before = await method.remaining(credential)
            assert before is not None
            await method.verify(credential, result.codes[0], Clock.utcnow())
            await db.commit()
            after = await method.remaining(credential)
            assert after == before - 1

    async def test_regenerating_destroys_the_previous_set(self):
        user = await make_user("backup-regen")
        async with DatabaseService.session() as db:
            method = BackupCodesMethod(db, settings())
            first = await method.begin_enrollment(user, "Backup codes")
            assert first.codes is not None
            await db.commit()

            second = await method.begin_enrollment(user, "Backup codes")
            assert second.codes is not None
            await db.commit()

            # Exactly one backup-codes credential survives regeneration —
            # ALLOWS_MULTIPLE = False means replace, not accumulate.
            credentials = await MfaCredential.list_for_user(db, user.id)
            assert len(credentials) == 1

            new_credential = await MfaCredential.get_for_user(
                db, user.id, second.credential_id
            )
            assert new_credential is not None
            assert not await method.verify(
                new_credential, first.codes[0], Clock.utcnow()
            )
            assert await method.verify(new_credential, second.codes[0], Clock.utcnow())

    async def test_backup_credential_needs_no_activation(self):
        user = await make_user("backup-no-activation")
        async with DatabaseService.session() as db:
            method = BackupCodesMethod(db, settings())
            result = await method.begin_enrollment(user, "Backup codes")
            await db.commit()
            credential = await MfaCredential.get_for_user(
                db, user.id, result.credential_id
            )
            assert credential is not None
            assert credential.is_active


class TestMfaMethodRegistry:
    def test_for_kind_returns_the_right_class(self):
        registry = MfaMethodRegistry(cast(AsyncSession, None), settings())
        assert isinstance(registry.for_kind(MfaMethodKind.TOTP), TotpMethod)
        assert isinstance(
            registry.for_kind(MfaMethodKind.BACKUP_CODES), BackupCodesMethod
        )

    def test_for_credential_returns_none_for_an_unknown_kind(self):
        registry = MfaMethodRegistry(cast(AsyncSession, None), settings())
        credential = MfaCredential(user_id=1, kind="carrier-pigeon", label="?")
        assert registry.for_credential(credential) is None

    def test_for_credential_returns_the_right_class_for_a_known_kind(self):
        registry = MfaMethodRegistry(cast(AsyncSession, None), settings())
        credential = MfaCredential(user_id=1, kind="totp", label="Phone")
        assert isinstance(registry.for_credential(credential), TotpMethod)

    def test_describe_names_every_registered_kind(self):
        described = {entry["kind"] for entry in MfaMethodRegistry.describe()}
        assert described == {"totp", "backup_codes"}


class TestMfaVerifier:
    async def test_required_kinds_ignores_unactivated_credentials(self):
        user = await make_user("verifier-unactivated")
        async with DatabaseService.session() as db:
            method = TotpMethod(db, settings())
            await method.begin_enrollment(user, "Phone")  # never activated
            await db.commit()

            verifier = MfaVerifier(db, settings())
            assert await verifier.required_kinds(user) == []
            assert not await verifier.is_enrolled(user)

    async def test_required_kinds_lists_activated_credentials(self):
        user = await make_user("verifier-activated")
        async with DatabaseService.session() as db:
            method = TotpMethod(db, settings())
            result = await method.begin_enrollment(user, "Phone")
            await db.commit()
            credential = await MfaCredential.get_for_user(
                db, user.id, result.credential_id
            )
            assert result.secret is not None
            assert credential is not None
            code = pyotp.TOTP(result.secret).now()
            await method.complete_enrollment(credential, code)
            await db.commit()

            verifier = MfaVerifier(db, settings())
            assert await verifier.required_kinds(user) == [MfaMethodKind.TOTP]
            assert await verifier.is_enrolled(user)

    async def test_verify_tries_every_activated_credential(self):
        user = await make_user("verifier-tries-all")
        async with DatabaseService.session() as db:
            totp_method = TotpMethod(db, settings())
            totp_result = await totp_method.begin_enrollment(user, "Phone")
            await db.commit()
            totp_credential = await MfaCredential.get_for_user(
                db, user.id, totp_result.credential_id
            )
            assert totp_result.secret is not None
            assert totp_credential is not None
            code = pyotp.TOTP(totp_result.secret).now()
            await totp_method.complete_enrollment(totp_credential, code)
            await db.commit()

            backup_method = BackupCodesMethod(db, settings())
            backup_result = await backup_method.begin_enrollment(user, "Backup codes")
            assert backup_result.codes is not None
            await db.commit()

            verifier = MfaVerifier(db, settings())
            assert await verifier.verify(user, backup_result.codes[0])

    async def test_verify_returns_false_for_a_wrong_code(self):
        user = await make_user("verifier-wrong-code")
        async with DatabaseService.session() as db:
            method = TotpMethod(db, settings())
            result = await method.begin_enrollment(user, "Phone")
            await db.commit()
            credential = await MfaCredential.get_for_user(
                db, user.id, result.credential_id
            )
            assert result.secret is not None
            assert credential is not None
            code = pyotp.TOTP(result.secret).now()
            await method.complete_enrollment(credential, code)
            await db.commit()

            verifier = MfaVerifier(db, settings())
            assert not await verifier.verify(user, "000000")


class TestMfaChallengeToken:
    """`mint`/`verify` directly, independent of any particular caller."""

    async def test_verify_returns_the_user_id_for_a_fresh_token(self):
        user = await make_user("challenge-fresh")
        token, expires_in = MfaChallengeToken.mint(
            settings(), user, MfaChallengeContext.TOKEN
        )
        assert expires_in > 0
        assert (
            MfaChallengeToken.verify(settings(), token, MfaChallengeContext.TOKEN)
            == user.id
        )

    async def test_verify_rejects_the_wrong_context(self):
        user = await make_user("challenge-wrong-context")
        token, _expires_in = MfaChallengeToken.mint(
            settings(), user, MfaChallengeContext.TOKEN
        )
        assert (
            MfaChallengeToken.verify(settings(), token, MfaChallengeContext.OAUTH)
            is None
        )

    def test_verify_rejects_a_malformed_token(self):
        assert (
            MfaChallengeToken.verify(
                settings(), "not-a-token", MfaChallengeContext.TOKEN
            )
            is None
        )


class TestConcurrentClaims:
    """Two requests presenting one code at the same moment.

    The guard is a conditional UPDATE rather than a compare in Python
    (MfaCredential.claim_totp_step), because two sessions both read the row
    before either writes — which is the real-time relay this exists to stop,
    an attacker submitting the code they just phished while its owner
    submits it too. Exercised through two sessions rather than two requests:
    the interleaving is what matters, and it is the same interleaving.
    """

    async def test_the_same_totp_code_is_accepted_only_once(self):
        user = await make_user("totp-concurrent")
        async with DatabaseService.session() as setup:
            result = await TotpMethod(setup, settings()).begin_enrollment(user, "Phone")
            await setup.commit()
        assert result.secret is not None

        now = Clock.utcnow()
        code = code_at(result.secret, now)

        async with (
            DatabaseService.session() as first,
            DatabaseService.session() as second,
        ):
            # Both load the row before either writes — the whole point.
            first_credential = await MfaCredential.get_for_user(
                first, user.id, result.credential_id
            )
            second_credential = await MfaCredential.get_for_user(
                second, user.id, result.credential_id
            )
            assert first_credential is not None and second_credential is not None

            accepted_first = await TotpMethod(first, settings()).verify(
                first_credential, code, now
            )
            await first.commit()
            accepted_second = await TotpMethod(second, settings()).verify(
                second_credential, code, now
            )
            await second.commit()

        assert accepted_first
        assert not accepted_second

    async def test_the_same_backup_code_is_spent_only_once(self):
        user = await make_user("backup-concurrent")
        async with DatabaseService.session() as setup:
            result = await BackupCodesMethod(setup, settings()).begin_enrollment(
                user, "Backup codes"
            )
            await setup.commit()
        assert result.codes is not None
        code = result.codes[0]

        now = Clock.utcnow()
        async with (
            DatabaseService.session() as first,
            DatabaseService.session() as second,
        ):
            first_credential = await MfaCredential.get_for_user(
                first, user.id, result.credential_id
            )
            second_credential = await MfaCredential.get_for_user(
                second, user.id, result.credential_id
            )
            assert first_credential is not None and second_credential is not None

            accepted_first = await BackupCodesMethod(first, settings()).verify(
                first_credential, code, now
            )
            await first.commit()
            accepted_second = await BackupCodesMethod(second, settings()).verify(
                second_credential, code, now
            )
            await second.commit()

        assert accepted_first
        assert not accepted_second


class TestNonAsciiCodes:
    """`str.isdigit()` is True for Arabic-Indic and full-width digits, and
    `hmac.compare_digest` raises TypeError rather than returning False on a
    non-ASCII str — so without the `isascii()` guard these are a 500 in the
    middle of a login rather than a refusal."""

    @pytest.mark.parametrize("code", ["١٢٣٤٥٦", "1234５6", "١23456"])
    async def test_a_non_ascii_code_is_refused_not_raised(self, code):
        user = await make_user(f"totp-non-ascii-{len(code)}-{code[0]}")
        async with DatabaseService.session() as db:
            method = TotpMethod(db, settings())
            result = await method.begin_enrollment(user, "Phone")
            await db.commit()
            credential = await MfaCredential.get_for_user(
                db, user.id, result.credential_id
            )
            assert credential is not None
            assert not await method.verify(credential, code, Clock.utcnow())


class TestChallengeBinding:
    def test_a_binding_mismatch_is_refused(self):
        user = User(id=1, username="bound", password="hash", roles=[])
        token, _ = MfaChallengeToken.mint(
            settings(), user, MfaChallengeContext.OAUTH, binding="client-a"
        )
        payload = MfaChallengeToken._validated_payload(
            settings(), token, MfaChallengeContext.OAUTH, "client-b"
        )
        assert payload is None

    def test_a_bound_token_is_refused_where_none_is_expected(self):
        """An unbound caller must not accept a bound token either: `bnd`
        absent reads as None, so the two are the same comparison."""
        user = User(id=1, username="bound", password="hash", roles=[])
        token, _ = MfaChallengeToken.mint(
            settings(), user, MfaChallengeContext.OAUTH, binding="client-a"
        )
        assert (
            MfaChallengeToken._validated_payload(
                settings(), token, MfaChallengeContext.OAUTH
            )
            is None
        )

    def test_a_matching_binding_passes(self):
        user = User(id=1, username="bound", password="hash", roles=[])
        token, _ = MfaChallengeToken.mint(
            settings(), user, MfaChallengeContext.OAUTH, binding="client-a"
        )
        payload = MfaChallengeToken._validated_payload(
            settings(), token, MfaChallengeContext.OAUTH, "client-a"
        )
        assert payload is not None
