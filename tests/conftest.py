"""Shared fixtures.

Everything the application reads from the environment is pinned here *before*
any application module is imported. This has to happen at module scope rather
than in a fixture because `services.config` would otherwise fall through to the
developer's own `.env` on its first, module-scoped read. Settings are set
explicitly rather than deleted, because an unset variable would still be picked
up from that `.env` file.

Nothing here depends on a value that exists outside this file — the database is
a fresh temporary file, the signing key is generated per run, and identifiers
are read back from the rows the fixtures create.
"""

import os
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parent.parent
_TMP_DIR = tempfile.mkdtemp(prefix="resume-api-tests-")

# Pinned rather than left to the default, so a developer whose .env says
# production does not get a different suite than CI. Tests that need production
# set it themselves; `fresh_settings` drops the cache around every test.
os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DIR}/test.db"
os.environ["DATABASE_ECHO"] = "false"
os.environ["AUTH_SECRET_KEY"] = secrets.token_urlsafe(32)
os.environ["AUTH_ALGORITHM"] = "HS256"
os.environ["AUTH_ACCESS_TOKEN_EXPIRE_MINUTES"] = "30"
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ["OAUTH_ALLOWED_REDIRECT_HOSTS"] = "claude.ai,chatgpt.com"
# Generated per run, like AUTH_SECRET_KEY above: the suite encrypts TOTP
# secrets the same way a deployment does, so a change that stops sealing
# them fails here rather than in production.
os.environ["MFA_ENCRYPTION_KEYS"] = Fernet.generate_key().decode()
# Empty rather than absent: these must override anything in a local .env, and
# an empty value is what the bootstrap treats as "not configured".
os.environ["BOOTSTRAP_ADMIN_USERNAME"] = ""
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = ""
os.environ["BOOTSTRAP_ADMIN_EMAIL"] = ""
# Same reasoning: a developer running the client locally has both of these in
# their own .env, and unpinned the suite reads whatever they happen to be.
os.environ["CLIENT_ALLOWED_ORIGINS"] = ""
os.environ["CLIENT_HTML_PATH"] = ""
os.environ.pop("SECRETS_DIR", None)

import pyotp
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

import main
from persistence.base import SQAlchemyBase
from persistence.mfa_credential import MfaCredential
from persistence.user import User
from services.auth.auth_service import AuthService
from services.auth.mfa.methods.totp import TotpMethod
from services.auth.roles import Roles
from services.auth.scopes import ScopeResolver
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    """Run the real migrations once, against the temporary database.

    Uses the project's own alembic.ini; env.py resolves the URL from
    DATABASE_URL, so no ini rewriting is needed.
    """
    from alembic.config import Config

    from alembic import command

    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")
    yield


@pytest.fixture(autouse=True)
async def fresh_settings():
    """Drop the memoised settings around every test.

    ConfigService caches ConfigServiceModel in a module global and reuses it for
    the life of the process, which is right for a server and wrong for a suite:
    without this, the first test to touch configuration would fix it for all the
    rest, and any test that adjusts the environment would silently have no
    effect. Reset afterwards too, so a test that sets something cannot leak it
    into the next one through the cache. `DatabaseService` is reset alongside
    it for the same reason, even though no test changes `DATABASE_URL` today.
    """
    ConfigService.reset()
    await DatabaseService.reset()
    yield
    ConfigService.reset()
    await DatabaseService.reset()


@pytest.fixture(autouse=True)
def fresh_role_scope_cache():
    """Drop the role-scope cache around every test.

    Mirrors `fresh_settings`: without this, the first test to authenticate
    pins the role->scope mapping for the whole run, and `clean_database`
    leaving `role_scopes` alone between tests would go unnoticed by tests that
    write to it directly.
    """
    ScopeResolver.reset_cache()
    yield
    ScopeResolver.reset_cache()


@pytest.fixture(autouse=True)
async def clean_database(migrated_database):
    """Empty every table between tests, except `role_scopes` and
    `document_schema`.

    Both are reference data owned by their migrations, not state a test run
    produces — wiping either would mean every test that reads scopes, or
    seeds a new account, re-seeding it by hand. Excluding them here is closer
    to the truth than restoring them would be.

    `documents` carries triggers that forbid DELETE, so they are dropped and
    restored around the wipe. Their definitions are read back out of
    sqlite_master rather than restated here, so this keeps working if a later
    migration changes them.
    """
    async with DatabaseService.engine().begin() as conn:
        result = await conn.execute(
            text("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")
        )
        triggers = result.fetchall()

        for name, _ in triggers:
            await conn.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
        for table in reversed(SQAlchemyBase.metadata.sorted_tables):
            if table.name in ("role_scopes", "document_schema"):
                continue
            await conn.execute(table.delete())
        for _, sql in triggers:
            await conn.execute(text(sql))
    yield


@pytest.fixture(scope="session")
def password() -> str:
    """Satisfies the complexity rules by construction, without being a literal."""
    return "Aa1!" + secrets.token_urlsafe(12)


@pytest.fixture(scope="session")
def hashed_password(password) -> str:
    """Hashed once per session. Argon2 is deliberately slow; paying it per test
    would dominate the runtime."""
    return AuthService.get_password_hash(password)


@pytest.fixture
async def client():
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@dataclass
class Actor:
    """A seeded user plus a live token, so tests can act as them directly."""

    username: str
    user_id: int
    password: str
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


async def _create_user(username: str, roles: list[str], hashed: str | None) -> int:
    async with DatabaseService.session() as db:
        user = User(username=username, password=hashed, roles=roles)
        db.add(user)
        await db.commit()
        return user.id


async def _login(client: AsyncClient, username: str, password: str) -> str:
    response = await client.post(
        "/token", data={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture
async def make_actor(client, password, hashed_password):
    """Create a user with the given roles and return them logged in."""

    async def factory(username: str, roles: list[str]) -> Actor:
        user_id = await _create_user(username, roles, hashed_password)
        token = await _login(client, username, password)
        return Actor(username, user_id, password, token)

    return factory


@pytest.fixture
async def admin(make_actor) -> Actor:
    return await make_actor("admin-user", [Roles.ADMIN.value])


@pytest.fixture
async def member(make_actor) -> Actor:
    """Holds the member role: every document scope, no users:admin.

    A machine client is no longer a role — it is one of these users with an
    API key narrowed to the scopes it needs — so what this fixture exists to
    cover is the one difference between the two roles.
    """
    return await make_actor("member-user", [Roles.MEMBER.value])


@pytest.fixture
async def roleless(make_actor) -> Actor:
    return await make_actor("roleless-user", [])


@pytest.fixture
async def other_owner(make_actor) -> Actor:
    """A second user who can write documents — distinct from `admin`, for
    ownership-isolation tests: two users each holding a document does not mean
    either can see the other's."""
    return await make_actor("other-owner", [Roles.ADMIN.value])


@dataclass
class Enrolled:
    """An actor with an activated TOTP credential, plus its secret — the
    secret only ever exists in the clear at enrolment time, same as it
    would for a real authenticator app."""

    actor: Actor
    secret: str


@pytest.fixture
async def enrolled(make_actor) -> Enrolled:
    actor = await make_actor("mfa-enrolled-user", [])
    async with DatabaseService.session() as db:
        user = await User.get_user_by_id(db, actor.user_id)
        assert user is not None
        method = TotpMethod(db, ConfigService.get_without_deps().settings)
        result = await method.begin_enrollment(user, "Authenticator app")
        await db.commit()
        credential = await MfaCredential.get_for_user(db, user.id, result.credential_id)
        assert credential is not None
        assert result.secret is not None
        # Activated with the *previous* step's code: tests call
        # `totp_code(secret)` for the current step, and activating with that
        # same step would make the replay guard refuse their first login.
        totp = pyotp.TOTP(result.secret)
        activation_code = totp.at(datetime.now(UTC), counter_offset=-1)
        await method.complete_enrollment(credential, activation_code)
        await db.commit()

    return Enrolled(actor=actor, secret=result.secret)


@pytest.fixture
def totp_code():
    """Current code for a secret, as an authenticator app would show it."""
    return lambda secret: pyotp.TOTP(secret).now()


@pytest.fixture
def resume_payload() -> dict:
    """A minimal but complete ResumePrivate. The marker strings are generated so
    a leak assertion cannot pass by coincidence. `lastUsed` cannot carry the
    random marker directly — it is `Iso8601`-constrained — so it gets a
    distinctive, otherwise-unused year instead.
    """
    marker = secrets.token_hex(8)
    return {
        "basics": {
            "name": "Test",
            "label": "Engineer",
            "tagline": "tagline",
            "email": f"private-{marker}@example.invalid",
            "phone": f"555-{marker}",
            "summary": "summary",
            "location": {"label": "Remote", "kind": "remote", "note": "note"},
            "profiles": [{"network": "site", "url": "https://example.invalid"}],
        },
        "work": [
            {
                "name": "Company",
                "location": "Remote",
                "startDate": "2020-01",
                "endDate": None,
                "description": "description",
                "position": "Engineer",
                "roleLocation": "Remote",
                "roles": [{"title": "Engineer", "startDate": "2020", "endDate": None}],
                "highlights": [
                    {
                        "id": "highlight-1",
                        "summary": "did a thing",
                        "specifics": [{"detail": "detail"}],
                        "tech": [f"tech-{marker}"],
                        "metrics": [
                            {
                                "figure": f"figure-{marker}",
                                "amount": "1",
                                "basis": "basis",
                            }
                        ],
                        "story": f"story-{marker}",
                    }
                ],
            }
        ],
        "skills": [
            {
                "name": "group",
                "keywords": [{"name": "Python", "level": "expert", "lastUsed": "1907"}],
            }
        ],
        "fineTuningData": {
            "narrative": {"voice": f"voice-{marker}"},
            "logistics": {"salaryExpectation": f"salary-{marker}"},
        },
    }


@pytest.fixture
def private_markers(resume_payload) -> list[str]:
    """Every value that must never appear in a public response."""
    basics = resume_payload["basics"]
    highlight = resume_payload["work"][0]["highlights"][0]
    keyword = resume_payload["skills"][0]["keywords"][0]
    fine_tuning = resume_payload["fineTuningData"]
    return [
        basics["email"],
        basics["phone"],
        highlight["tech"][0],
        highlight["metrics"][0]["figure"],
        highlight["story"],
        keyword["lastUsed"],
        fine_tuning["narrative"]["voice"],
        fine_tuning["logistics"]["salaryExpectation"],
    ]


@pytest.fixture
def withheld_resume_payload(resume_payload) -> dict:
    """`resume_payload` plus a second work entry marked `publish: false`.

    A separate fixture rather than editing `resume_payload` itself, so every
    existing assertion elsewhere — the CORS tests, the unflagged-projection
    guard — keeps exercising the ordinary, unwithheld path.
    """
    hidden_work = {
        **resume_payload["work"][0],
        "name": "Hidden Co",
        "publish": False,
        "highlights": [
            {**resume_payload["work"][0]["highlights"][0], "id": "hidden-highlight"}
        ],
    }
    return {
        **resume_payload,
        "work": [resume_payload["work"][0], hidden_work],
    }


@pytest.fixture
def metadata_payload() -> dict:
    """A minimal but complete ResumeMetadata."""
    return {
        "schema_version": "1.0.0",
        "readme": "what each field is for",
        "disclosure": {
            "never_publish": ["resume.work[].highlights[].tech"],
            "rationale": "internal scale and vendor stack",
        },
    }


@pytest.fixture
async def stored_metadata(client, admin, metadata_payload):
    """resume.metadata.json, written through its own route."""
    response = await client.post(
        "/documents/metadata",
        headers=admin.headers,
        json={
            "name": "resume.metadata.json",
            "revision_note": "initial",
            "data": metadata_payload,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def skill_payload() -> dict:
    """A minimal but complete ResumeSkill: only the fields without defaults,
    plus one entry in each list that the MCP assertions look for."""
    marker = secrets.token_hex(8)
    return {
        "schema_version": "1.0.0",
        "name": "fine-tune-resume",
        "title": "Fine-tune the resume for a posting",
        "description": f"description-{marker}",
        "readme": "read this first",
        "persona": "recruiter",
        "objective": "a tailored resume",
        "inputs": [
            {
                "key": "resume",
                "document": "resume.json",
                "holds": "the content",
                "on_missing": "stop and say so",
            }
        ],
        "procedure": [
            {
                "id": "load",
                "title": "Load the documents",
                "instructions": ["retrieve all three"],
                "blocking": True,
            }
        ],
        "guardrails": [
            {
                "id": "join-on-id-only",
                "severity": "never",
                "rule": "match on id",
            }
        ],
        "outputs": [
            {
                "artifact": "resume",
                "format": "docx",
                "file_name_template": "{name} - {title} - {company}.docx",
            },
            {
                "artifact": "cover-letter",
                "format": "docx",
                "file_name_template": "{name} - Cover Letter - {title} - {company}.docx",
            },
        ],
        "report_back": ["which bullet ids you used"],
    }


@pytest.fixture
async def stored_skill(client, admin, skill_payload):
    """resume.skill.json, written through its own route."""
    response = await client.post(
        "/documents/skill",
        headers=admin.headers,
        json={
            "name": "resume.skill.json",
            "revision_note": "initial",
            "data": skill_payload,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
async def stored_resume(client, admin, resume_payload):
    """resume.json, written through the API so the write path is real.

    Public by default, matching what PUBLIC_DOCUMENTS used to guarantee for
    the one document on that list — most tests care about the public route
    working, not about the flag itself.
    """
    response = await client.post(
        "/documents/resume",
        headers=admin.headers,
        json={
            "name": "resume.json",
            "revision_note": "initial",
            "public": True,
            "data": resume_payload,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()
