"""Branches that the HTTP surface cannot reach on its own.

Each of these guards a state the routes make unreachable — a principal whose
user vanished mid-request, a storage layer returning nothing, two instances
racing to claim a database. They are cheap to hold correct and expensive to
debug if they ever quietly stop working, so they are exercised directly.
"""

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from persistence.user import User
from services.auth.principal import CredentialKind, Principal
from services.auth.scopes import Scopes
from services.config.config_service import (
    ConfigService,
    ConfigServiceModel,
    UnexpectedNoSettings,
)
from services.database.database_service import DatabaseService
from services.document.document_service import DocumentService
from services.document.dtos.create_document import CreateDocumentRequest
from services.document.dtos.resume_object import ResumePrivate
from services.user.bootstrap import AdminBootstrapper, BootstrapOutcome
from services.user.dtos.update_user import UpdateUserRequest
from services.user.user_service import UserService


def principal_for(user_id: int, *scopes: Scopes) -> Principal:
    return Principal(
        user_id=user_id,
        username="detached",
        scopes=frozenset(scopes or (Scopes.RESUME_WRITE,)),
        credential=CredentialKind.JWT,
    )


class TestVanishedUser:
    """A Principal resolves before the service runs; the row could go away
    in between."""

    async def test_current_user_404s(self):
        async with DatabaseService.session() as db:
            service = UserService(
                db, principal_for(999_999), ConfigService.get_without_deps()
            )
            with pytest.raises(HTTPException) as caught:
                await service.get_current_user()
        assert caught.value.status_code == 404

    async def test_update_without_a_target_is_rejected(self, admin):
        async with DatabaseService.session() as db:
            service = UserService(
                db,
                principal_for(admin.user_id, Scopes.USERS_ADMIN),
                ConfigService.get_without_deps(),
            )
            with pytest.raises(HTTPException) as caught:
                await service.update_user(UpdateUserRequest(first_name="x"))
        assert caught.value.status_code == 400


class TestStorageFailure:
    async def test_upsert_returning_nothing_is_a_500(
        self, monkeypatch, admin, resume_payload
    ):
        from persistence.document import Document, UpsertResult

        async def returns_nothing(db, document, public=None):
            return UpsertResult(None, created=True)

        monkeypatch.setattr(Document, "upsert_document", returns_nothing)

        async with DatabaseService.session() as db:
            service = DocumentService(db, principal_for(admin.user_id))
            with pytest.raises(HTTPException) as caught:
                await service.upsert_resume_document(
                    CreateDocumentRequest[ResumePrivate](
                        name="resume.json",
                        revision_note="v",
                        data=ResumePrivate.model_validate(resume_payload),
                    )
                )
        assert caught.value.status_code == 500


class TestBootstrapRace:
    async def test_losing_the_race_reports_already_claimed(self, monkeypatch, password):
        """Two instances starting against the same fresh database."""

        async def raise_conflict(self):
            raise IntegrityError("INSERT", {}, Exception("duplicate"))

        async with DatabaseService.session() as db:
            monkeypatch.setattr(type(db), "commit", raise_conflict)
            outcome, created = await AdminBootstrapper(db).ensure_admin(
                "racer", password
            )

        assert outcome is BootstrapOutcome.ALREADY_CLAIMED
        assert created is None

    async def test_the_winner_is_the_one_that_persisted(self, password):
        async with DatabaseService.session() as db:
            await AdminBootstrapper(db).ensure_admin("winner", password)
        async with DatabaseService.session() as db:
            assert await User.get_user_by_username(db, "winner") is not None


class TestDatabaseServiceCaching:
    """The engine is built lazily, on first use, and cached until reset."""

    def test_builds_the_engine_on_first_use(self):
        assert DatabaseService._engine is None
        assert DatabaseService.engine() is not None
        assert DatabaseService._engine is not None

    def test_the_same_engine_is_handed_out_every_time(self):
        first = DatabaseService.engine()
        assert DatabaseService.engine() is first
        assert DatabaseService.session_factory().kw["bind"] is first

    async def test_reset_disposes_and_drops_the_cached_engine(self):
        first = DatabaseService.engine()
        pool_before = first.pool
        await DatabaseService.reset()
        assert DatabaseService._engine is None
        # dispose() swaps in a fresh pool, so a changed pool is what
        # distinguishes a disposed engine from an abandoned one.
        assert first.pool is not pool_before
        assert DatabaseService.engine() is not first


class TestConfigCaching:
    """Settings are read once and reused for the life of the process."""

    def test_builds_settings_on_first_use(self):
        assert ConfigService._cached is None
        assert ConfigService.get_with_deps().settings is not None
        assert ConfigService._cached is not None

    def test_the_same_instance_is_handed_out_every_time(self):
        first = ConfigService.get_with_deps().settings
        assert ConfigService.get_with_deps().settings is first
        assert ConfigService.get_without_deps().settings is first

    def test_both_accessors_populate_the_same_cache(self):
        settings = ConfigService.get_without_deps().settings
        assert ConfigService._cached is settings

    def test_a_preloaded_instance_is_reused_rather_than_rebuilt(self, monkeypatch):
        preloaded = ConfigServiceModel()
        monkeypatch.setattr(ConfigService, "_cached", preloaded)

        assert ConfigService.get_with_deps().settings is preloaded
        assert ConfigService.get_without_deps().settings is preloaded

    def test_environment_changes_need_a_restart(self, monkeypatch):
        """The cache is the point; this documents its cost."""
        original = ConfigService.get_with_deps().settings.auth_algorithm
        monkeypatch.setenv("AUTH_ALGORITHM", "HS512")

        assert ConfigService.get_with_deps().settings.auth_algorithm == original

    def test_a_cleared_cache_picks_up_the_new_environment(self, monkeypatch):
        ConfigService.get_with_deps()
        monkeypatch.setenv("AUTH_ALGORITHM", "HS512")
        ConfigService.reset()

        assert ConfigService.get_with_deps().settings.auth_algorithm == "HS512"

    def test_constructing_without_settings_is_refused(self):
        """Unreachable through the accessors, which always populate the cache
        first; this guards direct construction."""
        with pytest.raises(UnexpectedNoSettings):
            ConfigService(None)


class TestPasswordComplexityOnUpdate:
    """The update DTO restates the create rules; both need holding."""

    @pytest.mark.parametrize(
        "candidate,expected",
        [
            ("Sh0rt!", "at least 8 characters"),
            ("alllower1!", "uppercase"),
            ("ALLUPPER1!", "lowercase"),
            ("NoDigitsHere!", "digit"),
            ("NoSymbols1234", "special character"),
        ],
    )
    def test_each_rule_is_enforced(self, candidate, expected):
        with pytest.raises(ValueError, match=expected):
            UpdateUserRequest(password=candidate, password_retype=candidate)

    def test_a_valid_password_passes(self, password):
        assert (
            UpdateUserRequest(password=password, password_retype=password).password
            is not None
        )

    def test_password_is_optional(self):
        assert UpdateUserRequest(first_name="only-this").password is None

    def test_an_explicit_null_password_skips_the_rules(self):
        assert UpdateUserRequest(password=None, first_name="only-this").password is None


class TestRepr:
    """Used when a row shows up in a log line or a debugger."""

    async def test_api_key_repr_omits_the_hash(self, client, admin):
        from persistence.api_key import ApiKey

        created = (
            await client.post(
                "/api-keys",
                headers=admin.headers,
                json={"name": "loggable", "scopes": [Scopes.SKILL_READ.value]},
            )
        ).json()

        async with DatabaseService.session() as db:
            row = await ApiKey.get_by_id(db, created["api_key"]["id"])

        assert row is not None
        assert "loggable" in repr(row)
        assert row.key_hash not in repr(row)
        assert created["key"] not in repr(row)

    async def test_document_repr(self, admin, stored_resume):
        from persistence.document import Document

        async with DatabaseService.session() as db:
            document = await Document.get_latest(db, admin.user_id, "resume.json")
        assert "revision_id=1" in repr(document)

    async def test_user_repr_omits_the_password(self, admin):
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, admin.user_id)
        assert user is not None
        assert user.password is not None  # the admin fixture logs in, so it has one
        assert admin.username in repr(user)
        assert user.password not in repr(user)


class TestCliRace:
    async def test_reports_a_claim_that_landed_between_check_and_write(
        self, monkeypatch, admin, capsys
    ):
        """The CLI pre-checks for an admin before prompting; ensure_admin checks
        again, and that second check is what settles a concurrent claim."""
        import bootstrap_admin
        from bootstrap_admin import BootstrapAdminCommand

        # False for the CLI's pre-check, then the truth for ensure_admin's —
        # exactly what a claim landing in between looks like.
        answers = iter([False])

        async def stale_then_current(db, role):
            return next(answers, True)

        monkeypatch.setattr(User, "any_with_role", stale_then_current)
        monkeypatch.setattr("builtins.input", lambda *a: "late-arrival")
        monkeypatch.setattr(
            bootstrap_admin.getpass, "getpass", lambda *a: "Val1d!password"
        )

        assert await BootstrapAdminCommand().run() == 0
        assert "already exists" in capsys.readouterr().out


class TestValidationErrorsDoNotEchoInput:
    """FastAPI's stock 422 puts the offending value in an `input` key, so a
    body that fails validation on one field comes back carrying the rest of
    what was sent — and four routes here send a password. `SecretStr` does
    not help: `input` is the raw body as it arrived, before any field was
    parsed into one. See Application._handle_validation_error.
    """

    async def test_change_password_422_does_not_echo_the_password(self, client, admin):
        response = await client.post(
            "/users/me/password",
            headers=admin.headers,
            json={"current_password": admin.password, "new_password": "Aa1!aaaaaa"},
        )
        assert response.status_code == 422
        assert admin.password not in response.text

    async def test_mfa_enrolment_422_does_not_echo_the_password(self, client, admin):
        response = await client.post(
            "/users/me/mfa/totp",
            headers=admin.headers,
            json={"current_password": admin.password},
        )
        assert response.status_code == 422
        assert admin.password not in response.text

    async def test_the_error_shape_the_client_reads_is_intact(self, client, admin):
        """clients/web/index.html's parseValidationErrors reads `loc` and
        `msg` and nothing else — dropping `input` must not disturb those."""
        response = await client.post(
            "/users/me/mfa/totp",
            headers=admin.headers,
            json={"current_password": admin.password},
        )
        detail = response.json()["detail"]
        assert isinstance(detail, list)
        assert detail[0]["loc"] == ["body", "label"]
        assert detail[0]["msg"]
        assert "input" not in detail[0]
