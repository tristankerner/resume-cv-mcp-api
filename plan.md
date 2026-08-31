# plan.md — Stateless MFA for username/password login

Adds multi-factor authentication to every surface of `resume-mcp-api` that
accepts a username and password, plus the management UI in the browser client.

**Two repositories are touched.** They are separate git repos; the second is a
submodule of the first at `clients/`.

| Repo | Path | What changes |
| --- | --- | --- |
| `resume-mcp-api` | `/home/tristan/Workspaces/resume-mcp-api` | Everything in §1–§13 |
| `resume-mcp-api-clients` | `/home/tristan/Workspaces/resume-mcp-api-clients` | §14 (`web/index.html`, `web/README.md`) |

Commit them separately. Do not edit `resume-mcp-api/clients/web/index.html`
directly — that path is the submodule checkout of the second repo.

---

## §0. Decisions already made

These were settled before this plan was written. Do not re-litigate them.

1. **The challenge between step one and step two is stateless.** No
   `mfa_challenges` table. `POST /token` returns a short-lived *signed JWT*
   (the "MFA token") which the client posts back with the code. Nothing is
   written to the database to issue it.
2. **Replay defence is stateful.** Each TOTP credential row stores the last
   accepted time step, so a code cannot be used twice inside its validity
   window. Each backup code row stores `used_at`. This is credential state,
   not challenge state — it does not make the challenge stateful.
3. **TOTP comes from `pyotp`**, added as a runtime dependency. Do not
   hand-roll RFC 6238.
4. **The web client renders the enrolment QR itself**, from a third pinned
   CDN module carrying an SRI hash, matching the existing htm/preact and
   vanilla-jsoneditor pattern. The secret never leaves the page.
5. **The docs HTTP Basic prompt is replaced entirely** by an HTML
   login-then-MFA page, in the same shape as the OAuth authorize flow. Basic
   auth is removed from the codebase.
6. **TOTP secrets are encrypted at rest**, with Fernet from `cryptography`,
   under a key supplied separately from the database. See §4.8.

### §0.1 Why the secrets are encrypted, and what that does not buy

A TOTP seed is symmetric. Whoever reads one can generate valid codes for that
account forever, silently — unlike stripping MFA outright, which leaves a
visible absence. So it warrants more protection than a password hash needs.

Two facts make this cheap enough to do up front rather than later:

- **There is no new dependency.** `cryptography==50.0.0` is already pinned in
  `requirements.txt` and already in the deployed image, arriving via
  `fastmcp` → `fastmcp-slim[client,server]` → `authlib`/`joserfc`. §2 promotes
  it to a direct dependency, which adds nothing to the image.
- **There is nothing to migrate.** MFA does not exist yet, so there are no
  plaintext rows to backfill. Retrofitting this later would need a migration
  that runs application code to seal each row — and every migration in
  `alembic/versions/` is pure DDL — plus a dual-read period while it ran. Now
  it is two call sites.

**What it defends against:** a database read on its own. A leaked backup or
snapshot, SQL injection with read access, a managed-Postgres operator, a dump
pasted into a support ticket.

**What it does not defend against:** compromise of the application host. The
key is in the process, by construction. The claim is "a database read is no
longer sufficient", not "the secrets are safe", and `README.md` must say it
that way (§13) rather than implying more.

**The risk it introduces is availability**, and it is real: a lost or wrong
key locks every enrolled user out of every password login — worse, in
availability terms, than the confidentiality risk it removes. §4.8 designs
for that explicitly: format validation at settings load, a real startup check
against live data, a break-glass path that never decrypts, and backup codes
that keep working because they are hashed rather than encrypted.

---

## §1. What MFA means here, end to end

### §1.1 The three password surfaces

Every place a username and password are checked funnels through
`AuthService.authenticate_user` (`services/auth/auth_service.py:69`). There
are exactly three callers today:

| Surface | Caller | Today | After |
| --- | --- | --- | --- |
| `POST /token` | `routers/auth.py:31` | returns `Token` | returns `Token` **or** an MFA challenge; second step at `POST /token/mfa` |
| Production docs | `AuthService.require_docs_access:474` (HTTP Basic) | 401 + `WWW-Authenticate: Basic` | HTML login page at `/docs/login`, then an MFA page, then a signed cookie |
| `POST /oauth/authorize` | `OAuthService.complete_authorize:244` | login+consent form | same form, then a second MFA page carrying the protocol params forward |

All three get MFA. No fourth surface exists; API keys and OAuth
access/refresh tokens are not password logins and are untouched.

### §1.2 The user-visible flow (`/token`)

```
POST /token           username=alice&password=hunter2
  ├─ no MFA enrolled  → 200 {"access_token": "...", "token_type": "bearer"}
  └─ MFA enrolled     → 200 {"mfa_required": true,
                             "mfa_token": "<signed JWT, 5 min>",
                             "methods": ["totp", "backup_codes"],
                             "expires_in": 300}

POST /token/mfa       mfa_token=<...>&code=123456
  ├─ code good        → 200 {"access_token": "...", "token_type": "bearer"}
  ├─ code bad         → 401, and one failure charged to the account + address
  └─ token expired    → 401, "Start the login again."
```

Both steps are `application/x-www-form-urlencoded`, matching the existing
`/token` and the browser client's `request(..., {form})` helper.

**Why 200 and not 401 for the challenge.** The password *was* correct; the
request succeeded and produced a next step. A 401 would make the browser
client's `request()` helper clear the session and show "your session expired"
(`index.html:701`), which is wrong. Status 200 with a discriminated body is
the smallest change that keeps that helper honest.

**Why `methods` is a list of kinds, not credentials.** The caller does not
choose which authenticator to use — they type a code and the server tries
every active credential. Returning per-credential labels and ids before the
second factor is proven would leak more than the flow needs.

### §1.3 MFA is opt-in

A user with zero *activated* MFA credentials logs in exactly as they do
today, byte for byte. Every existing test that logs in keeps passing
unchanged; `tests/conftest.py`'s `make_actor` fixture is untouched.

### §1.4 The failure that must not be introduced

`AuthService._authenticate_jwt` (`auth_service.py:381`) decodes any token
signed with `AUTH_SECRET_KEY` and authenticates whoever `sub` names. The MFA
token and the docs-session cookie are both signed with that same key and both
carry `sub`. **Left alone, posting an MFA token as `Authorization: Bearer …`
would be a complete authentication bypass.**

`_looks_like_oauth_access_token` (`auth_service.py:289`) already routes on the
`token_use` claim, and its docstring records that *absence* of that claim is
what selects the password-JWT path. Extend that rule, in `_authenticate_jwt`:

```python
if payload.get("token_use") is not None:
    return None
```

This is mandatory, it is the highest-risk line in the whole change, and §12
requires a test for each new token kind proving it cannot authenticate.

---

## §2. Dependencies

`pyproject.toml`, `[project].dependencies`, alphabetically among the others:

```toml
    # Fernet, for the TOTP secrets at rest — see services/auth/mfa/secret_box.py.
    # Already in the tree transitively (fastmcp -> fastmcp-slim[client,server]
    # -> authlib/joserfc), so naming it here costs nothing in the image. Named
    # anyway: depending on somebody else's transitive dependency for your own
    # cryptography is how an unrelated upgrade removes it.
    "cryptography>=45.0.0",
    # RFC 6238 TOTP: code generation, the drift window, base32 secrets, and
    # the otpauth:// provisioning URI. Pure Python, no transitive
    # dependencies. Hand-rolling this is thirty lines of HMAC and a hundred
    # of encoding edge cases nobody reviews.
    "pyotp>=2.9.0",
```

The floor on `cryptography` is deliberately well below the 50.0.0 currently
resolved: `Fernet` and `MultiFernet` have been stable for a decade, and
pinning tighter than the API needs would only fight whatever `fastmcp` wants.

Then:

```bash
uv lock
```

`uv.lock` and `requirements.txt` are both checked in; regenerate whichever the
project's normal flow produces (`uv lock` writes `uv.lock`; if
`requirements.txt` is exported, `uv export --format requirements-txt >
requirements.txt`). `cryptography` should move from a transitive entry to a
direct one with no version change; `pyotp` is the only genuinely new package.

---

## §3. Persistence

Two new models, one migration.

### §3.1 `persistence/mfa_credential.py`

```python
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .base import SQAlchemyBase, utcnow


class MfaCredential(SQAlchemyBase):
    """One second factor belonging to a user.

    `kind` names which MfaMethod owns the row — see
    services/auth/mfa/registry.py. Everything method-specific hangs off that:
    a TOTP row uses `secret` and `last_used_step`, a backup-code row uses
    neither and owns a set of MfaBackupCode children instead. Adding SMS
    later adds a kind and a method class, not a column here, unless that
    method genuinely needs storage of its own.

    `activated_at` is NULL between "the secret was issued" and "the user
    proved they can produce a code from it". A NULL row satisfies nothing and
    is invisible to the login path — enrolling and then closing the tab must
    not lock anyone out.
    """

    __tablename__ = "mfa_credentials"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(nullable=False)
    label: Mapped[str] = mapped_column(nullable=False)
    # Sealed, not plaintext — MfaSecretBox owns the format and the "v1:"
    # prefix that marks it (§4.8). Nothing outside TotpMethod reads this
    # column, and nothing at all should compare it to a secret in hand.
    # Unbounded String, so a Fernet token (~100 base64 characters for a
    # 32-character base32 seed) needs no migration for length.
    secret: Mapped[str | None]
    # The TOTP time step most recently accepted for this credential. A code is
    # refused if its step is not strictly greater, which is what stops the
    # same six digits being replayed inside their thirty-second window.
    last_used_step: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    activated_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]

    def __repr__(self) -> str:
        return (
            f"MfaCredential(id={self.id!r}, kind={self.kind!r}, label={self.label!r})"
        )

    @property
    def is_active(self) -> bool:
        return self.activated_at is not None

    @staticmethod
    async def list_for_user(db: AsyncSession, user_id: int) -> list[MfaCredential]:
        result = await db.execute(
            select(MfaCredential)
            .where(MfaCredential.user_id == user_id)
            .order_by(MfaCredential.created_at)
        )
        return list(result.scalars().all())

    @staticmethod
    async def list_active_for_user(
        db: AsyncSession, user_id: int
    ) -> list[MfaCredential]:
        result = await db.execute(
            select(MfaCredential)
            .where(
                MfaCredential.user_id == user_id,
                MfaCredential.activated_at.is_not(None),
            )
            .order_by(MfaCredential.created_at)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_for_user(
        db: AsyncSession, user_id: int, credential_id: int
    ) -> MfaCredential | None:
        """Scoped to the owner deliberately: a caller may only name their own
        credential, so an id belonging to somebody else is simply not found."""
        return (
            (
                await db.execute(
                    select(MfaCredential).where(
                        MfaCredential.id == credential_id,
                        MfaCredential.user_id == user_id,
                    )
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def delete_all_for_user(db: AsyncSession, user_id: int) -> int:
        """Every credential, activated or not. Returns how many rows went.

        Children go first: SQLite does not enforce foreign keys by default,
        so relying on a cascade would leave orphaned backup codes there and
        not on Postgres — a difference the test suite would never see.
        """
```

Implement `delete_all_for_user` as: select the credential ids for the user,
`delete(MfaBackupCode).where(MfaBackupCode.credential_id.in_(ids))`, then
`delete(MfaCredential).where(MfaCredential.user_id == user_id)`, returning the
credential rowcount. It must **not** commit — the caller owns the commit, as
everywhere else in this codebase.

### §3.2 `persistence/mfa_backup_code.py`

```python
class MfaBackupCode(SQAlchemyBase):
    """One single-use recovery code, belonging to a backup-code credential.

    A child table rather than a JSON list on the credential: each code needs
    its own `used_at`, "how many are left" should be a count rather than a
    parse, and marking one used must not rewrite the whole set.

    Only the SHA-256 hash is stored, for the same reason API keys are hashed
    that way rather than with argon2 — these are generated secrets with no
    dictionary to attack, and the verification path must not be slow.
    """

    __tablename__ = "mfa_backup_codes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    credential_id: Mapped[int] = mapped_column(
        ForeignKey("mfa_credentials.id"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    used_at: Mapped[datetime | None]
```

Static methods needed:

- `list_unused(db, credential_id) -> list[MfaBackupCode]`
- `count_unused(db, credential_id) -> int`
- `delete_for_credential(db, credential_id) -> None` — used when a set is
  regenerated. Does not commit.

### §3.3 Register the models with Alembic

`alembic/env.py`, the import block at the top that exists "for their side
effect of registering on `SQAlchemyBase.metadata`":

```python
from persistence import (  # noqa: F401
    api_key,
    auth_failure,
    document,
    mfa_backup_code,
    mfa_credential,
    oauth_authorization_code,
    oauth_client,
    oauth_refresh_token,
    role_scope,
    user,
)
```

Missing this makes `alembic revision --autogenerate` propose dropping the new
tables — the comment in that file says so.

### §3.4 The migration

`alembic/versions/<hash>_add_mfa_credentials.py`. Find the current head with
`uv run alembic heads` and set `down_revision` to it — at the time of writing
the chain ends at `0d7b385e6a45` (`add_role_scopes`), but **verify rather than
assume**. Follow the style of
`alembic/versions/c1e5a7d92b30_add_login_throttling.py`: a real docstring
explaining what and why, additive only.

```python
def upgrade() -> None:
    op.create_table(
        "mfa_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("secret", sa.String(), nullable=True),
        sa.Column("last_used_step", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_mfa_credentials_user_id"), "mfa_credentials", ["user_id"])
    op.create_table(
        "mfa_backup_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("credential_id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["credential_id"], ["mfa_credentials.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_mfa_backup_codes_credential_id"),
        "mfa_backup_codes",
        ["credential_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_mfa_backup_codes_credential_id"), table_name="mfa_backup_codes"
    )
    op.drop_table("mfa_backup_codes")
    op.drop_index(op.f("ix_mfa_credentials_user_id"), table_name="mfa_credentials")
    op.drop_table("mfa_credentials")
```

Purely additive, so a migration applied ahead of the deploy is readable by the
code already running in front of it — the property the login-throttling
migration's docstring calls out.

`tests/conftest.py`'s `clean_database` fixture wipes every table in
`SQAlchemyBase.metadata.sorted_tables` order except `role_scopes`, so both new
tables are cleaned between tests with no fixture change.

---

## §4. The MFA method abstraction

This is requirement 3: methods must be expandable, behind a reusable
interface, with a repository that switches between them. New package:

```
services/auth/mfa/
    __init__.py
    kinds.py            MfaMethodKind
    methods/
        __init__.py
        method.py       MfaMethod (ABC) + EnrollmentResult
        totp.py         TotpMethod
        backup_codes.py BackupCodesMethod
    registry.py         MfaMethodRegistry
    secret_box.py       MfaSecretBox
    challenge.py        MfaChallengeToken
    verifier.py         MfaVerifier
    mfa_service.py      MfaService
    dtos/
        __init__.py
        mfa.py
```

Everything is a class method or instance method — `CLAUDE.md` forbids
free-standing functions. (`persistence/base.py:utcnow` is the one pre-existing
exception; do not add more.)

### §4.1 `kinds.py`

```python
from enum import StrEnum


class MfaMethodKind(StrEnum):
    """Which MfaMethod owns a credential row.

    Stored as a string in `mfa_credentials.kind`. An unrecognised value is
    treated as no method at all rather than raising — same rule as
    ScopeResolver.parse: a method retired from this enum should stop
    satisfying logins, not 500 every request from whoever still has a row.
    """

    TOTP = "totp"
    BACKUP_CODES = "backup_codes"
```

### §4.2 `methods/method.py` — the interface

```python
class EnrollmentResult(BaseModel):
    """What enrolment hands back to the caller, once.

    `secret` and `codes` are the only time these values exist outside the
    user's own device — nothing stores them recoverably, and neither is ever
    returned by a later read.
    """

    credential_id: int
    kind: MfaMethodKind
    label: str
    secret: str | None = None  # TOTP: the base32 seed
    otpauth_uri: str | None = None  # TOTP: what a QR encodes
    codes: list[str] | None = None  # backup codes: the plaintext set


class MfaMethod(ABC):
    """One kind of second factor.

    Bound to a database session and constructed by MfaMethodRegistry, never
    directly by a router or a service. Adding SMS means adding a subclass and
    one registry entry; nothing outside this package learns a new name.

    Every method mutates rows and never commits. The caller owns the
    transaction, matching AccountPolicy.register_failure and every other
    policy object in services/auth.
    """

    KIND: ClassVar[MfaMethodKind]
    LABEL: ClassVar[str]
    # Whether a user may hold more than one credential of this kind. True for
    # TOTP (a phone and a laptop are two authenticators); False for backup
    # codes, where a second set would only be an unbounded pile of live
    # secrets — regenerating replaces.
    ALLOWS_MULTIPLE: ClassVar[bool]
    # Whether enrolment is two-phase. TOTP is: the secret is worthless until
    # the user proves their authenticator produces codes from it, and marking
    # it active before that check is how people lock themselves out.
    REQUIRES_ACTIVATION: ClassVar[bool]

    def __init__(self, db: AsyncSession, settings: ConfigServiceModel):
        self.db = db
        self.settings = settings

    @abstractmethod
    async def begin_enrollment(self, user: User, label: str) -> EnrollmentResult:
        """Create the credential row and return the one-time material."""

    async def complete_enrollment(self, credential: MfaCredential, code: str) -> None:
        """Prove the user can produce a code, and activate the credential.

        Concrete and refusing by default: a method with
        REQUIRES_ACTIVATION = False has nothing to prove, and forcing it to
        implement an empty override would be noise.
        """
        raise MfaErrors.activation_not_required()

    @abstractmethod
    async def verify(self, credential: MfaCredential, code: str, now: datetime) -> bool:
        """Whether `code` satisfies this credential right now.

        Mutates the credential's replay state on success. Returns False for
        every ordinary refusal — a wrong code, a spent backup code, a replayed
        step — so the caller can charge one failure and move on without
        learning which.
        """

    async def remaining(self, credential: MfaCredential) -> int | None:
        """How many uses are left, for methods where that is finite.

        None means "not a countable thing" — a TOTP seed does not run out.
        The UI uses this to warn before somebody's last backup code is spent.
        """
        return None
```

`MfaErrors` lives in `services/auth/exceptions/__init__.py` alongside
`AuthErrors` — see §7.4.

### §4.3 `methods/totp.py`

```python
class TotpMethod(MfaMethod):
    """RFC 6238 TOTP, six digits on a thirty-second step.

    The whole of the algorithm comes from pyotp; what is here is enrolment,
    the drift window, and the replay guard — the three things a library
    cannot decide for a deployment.

    The only class in the codebase that handles a TOTP seed in the clear, and
    it holds one for the length of a method call. Everything on either side —
    the column, the DTOs, the registry — sees either a sealed string or
    nothing at all. See MfaSecretBox (§4.8).
    """

    KIND = MfaMethodKind.TOTP
    LABEL = "Authenticator app"
    ALLOWS_MULTIPLE = True
    REQUIRES_ACTIVATION = True

    def __init__(self, db, settings):
        super().__init__(db, settings)
        self.secret_box = MfaSecretBox.from_settings(settings)

    async def begin_enrollment(self, user, label):
        secret = pyotp.random_base32()
        credential = MfaCredential(
            user_id=user.id,
            kind=str(self.KIND),
            label=label,
            secret=self.secret_box.seal(secret),
            created_at=utcnow(),
        )
        self.db.add(credential)
        await self.db.flush()  # so credential.id exists; the caller commits
        uri = pyotp.TOTP(secret).provisioning_uri(
            name=user.username, issuer_name=self.settings.mfa_issuer
        )
        return EnrollmentResult(
            credential_id=credential.id,
            kind=self.KIND,
            label=label,
            secret=secret,
            otpauth_uri=uri,
        )

    async def complete_enrollment(self, credential, code):
        if credential.is_active:
            raise MfaErrors.already_activated()
        if not await self.verify(credential, code, utcnow()):
            raise MfaErrors.invalid_code()
        credential.activated_at = utcnow()

    async def verify(self, credential, code, now):
        if not credential.secret:
            return False
        cleaned = self._clean(code)
        if not cleaned.isdigit():
            return False

        secret = self.secret_box.open(credential.secret)
        if secret is None:
            # The stored value will not open under any configured key. Refused
            # rather than raised: this is the wrong-key failure (§4.8), and a
            # login path that 500s on it tells an unauthenticated caller more
            # about the deployment than a refusal does. `verify_mfa_key` is
            # what is supposed to catch this, at startup, before anyone tries.
            return False

        totp = pyotp.TOTP(secret)
        drift = self.settings.mfa_totp_drift_steps
        for offset in range(-drift, drift + 1):
            expected = totp.at(now, counter_offset=offset)
            if not hmac.compare_digest(expected, cleaned):
                continue
            step = totp.timecode(now) + offset
            # Strictly greater: a code already accepted cannot be presented
            # again, not even within the same thirty-second step it was
            # minted for, and an earlier step inside the drift window cannot
            # be walked backwards.
            if (
                credential.last_used_step is not None
                and step <= credential.last_used_step
            ):
                return False
            credential.last_used_step = step
            credential.last_used_at = now
            # Lazy re-seal, so key rotation completes itself as people log in
            # rather than needing a sweep. Free: this row is already dirty
            # from the two lines above, so it costs no extra write.
            credential.secret = self.secret_box.reseal(credential.secret)
            return True
        return False

    @staticmethod
    def _clean(code: str) -> str:
        """Authenticator apps and the people reading them both add spaces."""
        return code.strip().replace(" ", "")
```

Notes for the implementer:

- `pyotp.TOTP.at(for_time, counter_offset=…)` and `pyotp.TOTP.timecode(for_time)`
  are both public API. `timecode` returns the integer step.
- `now` arrives from `persistence.base.utcnow()`, which is **naive UTC** by
  project convention. pyotp treats a naive datetime as local time. Convert at
  the boundary: pass `now.replace(tzinfo=UTC)` into `at()` and `timecode()`, or
  compute a unix timestamp with `calendar.timegm(now.timetuple())`. Pick one
  and use it in both calls, or the drift window and the recorded step will
  disagree by the local UTC offset. **Write a test that fails if the process
  timezone is not UTC** (`monkeypatch.setenv("TZ", "America/New_York")` plus
  `time.tzset()`), because CI runs in UTC and this bug would not otherwise
  surface until deployment.
- `hmac.compare_digest` rather than `==`. pyotp's own `verify` does constant-
  time comparison, but it does not report which step matched, which the replay
  guard needs.

### §4.4 `methods/backup_codes.py`

```python
class BackupCodesMethod(MfaMethod):
    """A printed set of single-use recovery codes.

    The way back in when the authenticator is gone but the account is not.
    Generating a set replaces any previous one outright: two live sets is
    twice the secret material for no extra recovery.
    """

    KIND = MfaMethodKind.BACKUP_CODES
    LABEL = "Backup codes"
    ALLOWS_MULTIPLE = False
    REQUIRES_ACTIVATION = False

    CODE_BYTES: ClassVar[int] = 5  # 10 hex characters
```

- `begin_enrollment` — delete any existing backup-code credential for the user
  and its children (so regeneration is one path, not two), create a fresh
  credential with `activated_at=utcnow()` (nothing to prove), generate
  `settings.mfa_backup_code_count` codes via `secrets.token_hex(CODE_BYTES)`,
  store `ApiKeyToken.hash_secret(code)` for each, and return the plaintext set
  in `EnrollmentResult.codes`.
  - Reuse `services.auth.api_keys.ApiKeyToken.hash_secret` rather than calling
    `hashlib.sha256` again. It is the project's existing "hash a generated
    secret" helper and its docstring already argues why SHA-256 and not argon2.
  - Format codes for humans: `"-".join(...)` in two groups, e.g.
    `a1b2c-3d4e5`. Strip separators on input in `verify`.
- `verify` — clean the input (lowercase, strip `-` and whitespace), hash it,
  and look for an unused `MfaBackupCode` on this credential with a matching
  hash using `hmac.compare_digest` against each candidate. On a match set
  `used_at = now`, set `credential.last_used_at = now`, return True.
  - Compare against every unused row rather than querying by hash. The set is
    ten rows; a hash lookup is not worth an index, and iterating keeps the
    comparison constant-time per candidate.
- `remaining` — `await MfaBackupCode.count_unused(self.db, credential.id)`.

### §4.5 `registry.py` — the repository

```python
class MfaMethodRegistry:
    """Resolves a credential kind to the object that knows how to work it.

    The repository requirement: everything outside this package names a
    `MfaMethodKind`, never a class. Adding SMS is a module in methods/ plus
    one line in `_METHODS` — no router, no service and no DTO learns a new
    name, and nothing has to be searched for a place that switches on kind,
    because this is the only one.
    """

    _METHODS: ClassVar[dict[MfaMethodKind, type[MfaMethod]]] = {
        MfaMethodKind.TOTP: TotpMethod,
        MfaMethodKind.BACKUP_CODES: BackupCodesMethod,
    }

    def __init__(self, db: AsyncSession, settings: ConfigServiceModel):
        self.db = db
        self.settings = settings

    def for_kind(self, kind: MfaMethodKind) -> MfaMethod:
        return self._METHODS[kind](self.db, self.settings)

    def for_credential(self, credential: MfaCredential) -> MfaMethod | None:
        """None for a row whose kind is no longer a method this build knows.

        Same rule as ScopeResolver.parse and for_roles: a retired method
        stops satisfying logins rather than 500ing every request from
        whoever still holds a row of that kind.
        """
        try:
            kind = MfaMethodKind(credential.kind)
        except ValueError:
            return None
        return self.for_kind(kind)

    @classmethod
    def kinds(cls) -> list[MfaMethodKind]:
        return list(cls._METHODS)

    @classmethod
    def describe(cls) -> list[dict[str, object]]:
        """What the client needs to render an "add a method" menu, without
        hardcoding the list in two languages."""
        return [
            {
                "kind": str(kind),
                "label": method.LABEL,
                "allows_multiple": method.ALLOWS_MULTIPLE,
            }
            for kind, method in cls._METHODS.items()
        ]
```

### §4.6 `challenge.py` — the stateless MFA token

```python
class MfaChallengeContext(StrEnum):
    """Which surface minted a challenge.

    A challenge issued by the docs login must not be redeemable at /token,
    and vice versa: the three surfaces grant different things, and a token
    that works everywhere would let the weakest of them stand in for the
    strongest.
    """

    TOKEN = "token"
    OAUTH = "oauth"
    DOCS = "docs"


class MfaChallengeToken:
    """Mint and verify the short-lived proof that a password was accepted.

    A signed JWT and not a database row: nothing needs revoking (it lives
    five minutes), nothing needs pruning, and a scale-to-zero deployment pays
    no write for a login that has not finished. Bound to the password hash in
    force when it was minted, so changing the password invalidates every
    challenge outstanding against the old one.
    """

    TOKEN_USE: ClassVar[str] = "mfa_pending"
```

Claims:

| Claim | Value |
| --- | --- |
| `sub` | `str(user.id)` — matching the access token's convention |
| `token_use` | `"mfa_pending"` |
| `ctx` | an `MfaChallengeContext` value |
| `pwb` | password binding: `hashlib.sha256(user.password.encode()).hexdigest()[:32]` |
| `iat`, `exp` | `exp = iat + MFA_CHALLENGE_TTL_MINUTES` |
| `jti` | `secrets.token_urlsafe(8)`, for log correlation only |

API:

- `mint(settings, user, context) -> tuple[str, int]` — returns the encoded
  token and its lifetime in seconds (for `expires_in`).
- `verify(settings, token, context) -> int | None` — decode with the same key
  and algorithm the access token uses, reject if `token_use != TOKEN_USE`,
  reject if `ctx != context`, and return the user id. Returns `None` for
  every failure (expired, wrong signature, wrong context, malformed) —
  callers turn that into one indistinguishable 401.
- `password_binding(user) -> str` — the `pwb` computation, so minting and
  checking share one definition. The caller compares it after loading the
  user, because the check needs the row.

Verification order in the caller (`MfaVerifier`, §4.7): decode → load user →
`user is None or not user.active` → reject; `pwb` mismatch → reject.

### §4.7 `verifier.py` — the login-side entry point

```python
class MfaVerifier:
    """Whether a user's second factor is required, and whether a code satisfies it.

    Deliberately separate from MfaService: this runs on the login path with
    no Principal in hand — there is no authenticated caller yet — while
    MfaService is request-scoped behind a Principal and manages enrolment.
    Sharing one class would mean a class that is sometimes authenticated.
    """

    def __init__(self, db: AsyncSession, settings: ConfigServiceModel):
        self.db = db
        self.settings = settings
        self.registry = MfaMethodRegistry(db, settings)

    async def required_kinds(self, user: User) -> list[MfaMethodKind]:
        """The kinds this user has an activated credential for, deduped and
        ordered. Empty means no second factor is demanded."""

    async def is_enrolled(self, user: User) -> bool:
        return bool(await self.required_kinds(user))

    async def verify(self, user: User, code: str) -> bool:
        """Try `code` against every activated credential the user holds.

        Tried rather than selected: the caller types a code, not a method.
        A backup code and a TOTP code are different shapes, but sniffing the
        shape is a guess, and every candidate check here is one HMAC.

        Mutates whichever credential accepted it (replay step, or a spent
        backup code) and commits — this is the end of the login transaction
        and there is nothing else pending.
        """
```

`verify` must iterate **all** activated credentials even after one method
returns False, and must not short-circuit on the first `kind`. It returns on
the first success. If `for_credential` returns None (unknown kind), skip that
row.

### §4.8 `secret_box.py` — TOTP secrets at rest

Decision 6 in §0, with its threat model in §0.1. Read that first; this is the
mechanism, not the argument.

```python
class MfaSecretBox:
    """Seals a TOTP secret for storage and opens it to check a code.

    Fernet rather than raw AES-GCM: GCM needs a nonce per encryption that
    must be stored and must never repeat, and not having to manage nonces is
    the entire reason to reach for a high-level primitive here. MultiFernet
    carries rotation — every configured key can open, the first one seals.

    Unconfigured, this is a null box: `seal` returns its input and `open`
    returns it back. That is the development default, so a local run needs no
    key material to enrol an authenticator; §11 makes a key mandatory in
    production, where the model validator refuses to start without one.
    """

    # Marks a value as sealed. Fernet tokens already begin with a version
    # byte, but reading that means knowing Fernet's wire format — an explicit
    # prefix is what lets `is_sealed` be honest without decoding anything,
    # and what lets a v2 exist later without guessing.
    PREFIX: ClassVar[str] = "v1:"
```

The API, all instance methods except where noted:

| Method | Behaviour |
| --- | --- |
| `from_settings(settings)` (classmethod) | Builds a box from `settings.mfa_encryption_keys`. Empty list → null box. |
| `seal(plaintext) -> str` | `PREFIX + MultiFernet.encrypt(...)`, or `plaintext` unchanged on a null box. |
| `open(stored) -> str \| None` | Prefixed → decrypt under any configured key. Unprefixed → return as-is (a development row, or a deployment that added a key later). **Never raises** — returns `None` for a token that will not open. |
| `reseal(stored) -> str` | The value re-sealed under the *primary* key. Used by `TotpMethod.verify` for lazy rotation. Returns `stored` unchanged if it cannot be opened, so a failure here can never destroy a secret. |
| `is_sealed(stored) -> bool` (staticmethod) | Whether the value carries `PREFIX`. |

`open` swallowing `InvalidToken` and returning `None` is the load-bearing
choice: this runs on the login path, and an exception there is a 500 that
tells an unauthenticated caller the deployment's key is wrong. `TotpMethod`
turns `None` into an ordinary refusal.

#### The startup check

Format validation (§11) catches a malformed key. It cannot catch a key that
is well-formed and *wrong* — and that is the failure that locks everyone out.
Only real data can catch that, so check against real data.

Add to `StartupTasks` in `main.py`, and call it from `_lifespan` after
`bootstrap_admin_user` and before the prune sweeps:

```python
    async def verify_mfa_key(self) -> None:
        """Open one sealed TOTP secret, to prove the configured key is the
        one this database was written with.

        A wrong key is otherwise silent until somebody tries to log in, at
        which point every enrolled account is locked out at once and the
        cause is several layers away from the symptom. Refusing to start is
        the same call `bootstrap_admin_user` makes when it cannot create the
        admin it was configured with: a deployment that cannot serve its
        users should fail visibly rather than serve them badly.

        Nothing to check on a database with no sealed rows, which is every
        database before the first enrolment — so this is silent on a fresh
        deployment and only ever speaks when it has evidence.
        """
```

Implementation: select one `MfaCredential` whose `secret` starts with
`MfaSecretBox.PREFIX` (`LIKE 'v1:%'`, `LIMIT 1`). If there is none, return. If
there is one and `MfaSecretBox.from_settings(...).open(...)` returns `None`,
raise — a bare `RuntimeError` naming `MFA_ENCRYPTION_KEYS` is enough; it
propagates out of `_lifespan` and the process fails to start, exactly as a
`BootstrapError` does today.

#### Recovery when the key is genuinely lost

Three things must hold, and each is a requirement on work described elsewhere
in this plan:

1. **`python -m reset_mfa` never decrypts** (§8.2). It deletes rows. It is the
   break-glass path precisely because it does not need the key, and any change
   that makes it read a secret breaks that property.
2. **Backup codes survive a lost key.** They are SHA-256 *hashed*, not
   encrypted (§4.4), so a user holding one can still log in and re-enrol. This
   falls out of the split design rather than being engineered, and should stay
   that way deliberately — do not "unify" the two storage schemes.
3. **The key is backed up separately from the database.** Storing it with the
   dump reproduces exactly the threat it was introduced to remove. This is a
   deployment note, and §13 requires it in `README.md`.

---

## §5. Wiring MFA into `AuthService`

`services/auth/auth_service.py`.

### §5.1 `_authenticate_jwt` — the bypass fix (§1.4)

```python
    async def _authenticate_jwt(self, token: str) -> Principal | None:
        try:
            payload = jwt.decode(...)
        except InvalidTokenError:
            return None

        # This branch is reached by elimination — see
        # `_looks_like_oauth_access_token` — so it must refuse anything that
        # names a purpose of its own. An MFA challenge and a docs-session
        # cookie are signed with this same key and carry `sub`; without this
        # line either one presented as a bearer token would authenticate as
        # its subject, which is the whole account.
        if payload.get("token_use") is not None:
            return None

        user = await self._active_user_from_subject(payload)
        ...
```

### §5.2 `verify_current_password` — extracted, then reused

`change_password` (`auth_service.py:220`) currently inlines "check the lockout,
verify the password, charge a failure or clear the history". The MFA
management routes (§7) need exactly that, so extract it:

```python
    async def verify_current_password(self, user: User, password: str) -> None:
        """Check a password on an already-authenticated request, throttled the
        same way /token is.

        Left unthrottled, any route that takes a current password is a
        password oracle for whoever holds a token for the account. Sharing
        the account and address counters with /token is the right coupling:
        a lockout tripped here blocks /token and vice versa, because both are
        ways of proving you know the password.

        Raises rather than returning a bool: every caller's response to a
        wrong password is identical, and a bool is one `if` away from being
        forgotten.
        """
```

Body: exactly the first half of the existing `change_password` —
`account_policy`/`address_policy`/`address`/`now`, the `LockKind` checks, the
`verify_password` call with `_register_login_failure` and
`AuthErrors.wrong_current_password()`, then `_clear_login_failures(user)`.

`change_password` then becomes:

```python
    async def change_password(self, user, current_password, new_password) -> None:
        await self.verify_current_password(user, current_password)
        user.password = self.get_password_hash(new_password)
        await self.db.commit()
```

Its existing docstring moves to `verify_current_password` (rewritten as
above); `change_password` keeps a one-line one. `tests/test_users.py` and
`tests/test_lockout.py` must keep passing untouched — this is a pure
extraction and the observable behaviour is identical.

### §5.3 `register_mfa_failure`

A wrong MFA code must cost the same as a wrong password, or a six-digit code
is guessable in a few thousand requests.

```python
    async def register_mfa_failure(self, user: User) -> None:
        """Charge a failed second factor to the account and the address.

        The same counters /token uses, deliberately: a code is a credential
        and guessing at one is guessing at the account. Six digits is a
        million-wide space, which sounds large and is not — at the default
        five-per-fifteen-minutes it takes centuries, and unthrottled it takes
        an afternoon.

        Raises AuthErrors.account_locked* when this attempt trips a lock,
        exactly as _register_login_failure does on the password path.
        """
        account_policy = AccountPolicy.from_settings(self.config_services.settings)
        address_policy = AddressPolicy.from_settings(self.config_services.settings)
        address = address_policy.client_address(self.request)
        await self._register_login_failure(
            user, address, account_policy, address_policy, utcnow()
        )
```

And on success the second step clears the history the same way a correct
password does — expose `clear_login_failures` as a public wrapper around the
existing `_clear_login_failures`, or simply call the private one from the
login service inside the same package. Prefer a public
`async def clear_login_failures(self, user: User) -> None` delegating to the
private one, so the underscore convention is not violated across module lines.

### §5.4 Remove the Basic docs scheme

`require_docs_access` (`auth_service.py:473`) and `docs_basic_scheme`
(`auth_service.py:39`) are replaced by the cookie-based dependency in §9.
Delete both from `AuthService`, and delete `AuthErrors.docs_credentials()`
(`services/auth/exceptions/__init__.py:16`) once nothing references it —
dead code costs coverage against the 90% target.

Also remove the now-unused `HTTPBasic`/`HTTPBasicCredentials` imports.

---

## §6. `POST /token` and `POST /token/mfa`

### §6.1 DTOs — `services/auth/dtos/mfa.py`

```python
class MfaRequiredResponse(BaseModel):
    """The first step's answer when the account has a second factor.

    `mfa_required` is a literal True rather than a bool so a client can
    discriminate the union on it without inspecting which keys are present.
    """

    mfa_required: Literal[True] = True
    mfa_token: str
    methods: list[MfaMethodKind]
    expires_in: int
```

`services/auth/token_data.py` keeps `Token` unchanged.

### §6.2 `services/auth/login_service.py`

The `/token` route currently holds real logic in the router
(`routers/auth.py:31-42`), against the project's stated convention that
routers stay thin. Two-step login makes that worse, so move it:

```python
class LoginService(ServiceProviderInterface):
    """The password login, both steps.

    Owns the branch that /token used to make inline: password accepted plus
    no second factor is a token, password accepted plus a second factor is a
    challenge, and redeeming that challenge is the same token by a different
    door.
    """

    def __init__(self, db, auth_service, config_service): ...

    async def login(self, username: str, password: str) -> Token | MfaRequiredResponse:
        user = await self.auth_service.authenticate_user(username, password)
        if not user:
            raise AuthErrors.credentials()

        verifier = MfaVerifier(self.db, self.settings)
        kinds = await verifier.required_kinds(user)
        if not kinds:
            return self._issue(user)

        token, expires_in = MfaChallengeToken.mint(
            self.settings, user, MfaChallengeContext.TOKEN
        )
        return MfaRequiredResponse(
            mfa_token=token, methods=kinds, expires_in=expires_in
        )

    async def complete_mfa(self, mfa_token: str, code: str) -> Token:
        user = await self._user_from_challenge(mfa_token, MfaChallengeContext.TOKEN)
        verifier = MfaVerifier(self.db, self.settings)
        if not await verifier.verify(user, code):
            await self.auth_service.register_mfa_failure(user)
            raise AuthErrors.credentials()
        await self.auth_service.clear_login_failures(user)
        return self._issue(user)
```

- `_issue(user)` holds what `routers/auth.py` does today: the
  `auth_access_token_expire_minutes` timedelta and
  `auth_service.create_access_token({"sub": str(user.id)}, …)`, returning
  `Token(access_token=…, token_type="bearer")  # nosec B106`. Keep the
  `nosec` comment — bandit runs in CI.
- `_user_from_challenge(token, context)` — verify via `MfaChallengeToken`,
  load the user, check `active`, check the `pwb` binding, and raise
  `AuthErrors.credentials()` on any failure. Also re-check the account lock
  (`AccountPolicy.status`) before accepting a code, so a lock tripped between
  the two steps is honoured.
- Shared by the docs login (§9) and reusable by OAuth (§8) — put
  `_user_from_challenge` somewhere both can reach. Cleanest: make it a method
  on `MfaVerifier` named `user_from_challenge(token, context)` returning
  `User | None`, and let each surface turn None into its own refusal shape
  (JSON 401, HTML re-render, redirect). Do that rather than importing
  `LoginService` into the OAuth service.

### §6.3 `routers/auth.py`

```python
class AuthRouter:
    LoginServiceDep = Annotated[LoginService, Depends(LoginService.get_with_deps)]
    FormDep = Annotated[OAuth2PasswordRequestForm, Depends()]

    def _register(self) -> None:
        self.router.post("/token", response_model=None)(self.login_for_access_token)
        self.router.post("/token/mfa", response_model=None)(self.complete_mfa)

    async def login_for_access_token(
        self, login_service: LoginServiceDep, form_data: FormDep
    ) -> Token | MfaRequiredResponse:
        return await login_service.login(form_data.username, form_data.password)

    async def complete_mfa(
        self,
        login_service: LoginServiceDep,
        mfa_token: Annotated[str, Form()],
        code: Annotated[str, Form()],
    ) -> Token:
        return await login_service.complete_mfa(mfa_token, code)
```

`response_model=None` on `/token` is required — FastAPI cannot build a single
response model for the union, and without it the `Token` model would strip the
MFA fields out of the response body silently. `routers/oauth.py` already uses
`response_model=None` for the same reason; follow that precedent.

`ConfigService` is no longer needed in this router; the expiry moves into
`LoginService`.

---

## §7. Self-service MFA management

Requirement 6. New router, new service, new DTOs.

### §7.1 Routes — `routers/mfa.py`

All under `APIRouter(prefix="/users/me/mfa", tags=["mfa"])`.

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `GET` | `` | — | `MfaStatusResponse` |
| `POST` | `/totp` | `{label, current_password}` | `EnrollTotpResponse` |
| `POST` | `/totp/{credential_id}/activate` | `{code}` | 204 |
| `POST` | `/backup-codes` | `{current_password}` | `BackupCodesResponse` |
| `DELETE` | `/{credential_id}` | `{current_password}` | 204 |

**`DELETE` with a request body** is unusual but legal, FastAPI supports it,
and the browser client's `request()` helper already passes `body` independently
of method (`index.html:672`). The alternative — a `POST …/remove` — reads
worse. Say so in a comment on the route.

### §7.2 Authorization rules

Every route here calls `self.principal.require_interactive()` first. An API
key must not be able to enrol or remove a second factor: the reasoning is
identical to `Principal.require_interactive`'s existing docstring about key
management and recovery fields — a key that can strip MFA makes MFA a
suggestion. Extend that docstring to name MFA management alongside them.

Every route **except `GET`** additionally requires `current_password`, checked
through `AuthService.verify_current_password` (§5.2) so the throttle applies.

- Enrolment needs it because adding a factor to a hijacked session is how an
  attacker locks the real owner out.
- Removal needs it because removing a factor is the attack in the other
  direction.
- Reading status does not, because it discloses nothing the caller does not
  already know about their own account.

### §7.3 `services/auth/mfa/mfa_service.py`

```python
class MfaService(ServiceProviderInterface):
    """Enrolment and removal, for the authenticated owner of the account.

    Only ever acts on `principal.user_id`. There is no admin path through
    here — an admin clearing somebody else's factors goes through
    UserService.reset_mfa (§8), which is a different operation with a
    different justification and deserves to be findable as one.
    """

    def __init__(self, db, principal, auth_service, config_service): ...

    async def status(self) -> MfaStatusResponse
    async def enroll_totp(self, request: EnrollTotpRequest) -> EnrollTotpResponse
    async def activate_totp(self, credential_id: int, code: str) -> None
    async def regenerate_backup_codes(self, request: PasswordConfirmRequest) -> BackupCodesResponse
    async def remove(self, credential_id: int, request: PasswordConfirmRequest) -> None

    @staticmethod
    def get_with_deps(db, principal, auth_service, config_service) -> MfaService: ...
```

Behaviour details that matter:

- **`status`** returns, for each credential: `id`, `kind`, `label`,
  `created_at`, `activated_at`, `last_used_at`, and `remaining` (from
  `MfaMethod.remaining`). Plus `enrolled: bool` (any activated credential),
  `available_methods` (from `MfaMethodRegistry.describe()`), and
  `backup_codes_remaining: int | None`. It **never** returns a secret, an
  otpauth URI, or a backup code — those exist once, in the response that
  created them.
- **`enroll_totp`** refuses a `label` that is blank after stripping (mirror
  `CreateApiKeyRequest.strip_name`). Cap the number of credentials per user
  at a small constant (`MfaService.MAX_CREDENTIALS = 10`) so enrolment is not
  an unbounded write for whoever holds a session.
- **`activate_totp`** loads via `MfaCredential.get_for_user` (owner-scoped, so
  someone else's id is a 404, matching the `ApiKeyService.revoke` precedent at
  `api_key_service.py:88`), refuses a credential of the wrong kind, and calls
  `method.complete_enrollment`, then commits.
  - A failed activation code is *not* charged to the login throttle. The
    caller is already authenticated and holds no advantage from guessing at a
    secret they were just handed. Say so in a comment; it is the kind of
    asymmetry a reviewer will otherwise flag.
- **`regenerate_backup_codes`** is also how backup codes are first created —
  one route, not an enrol/regenerate pair, because `ALLOWS_MULTIPLE = False`
  makes the two operations identical.
- **`remove`** deletes the credential and any children. It does **not** refuse
  to remove the last factor: MFA is optional (requirement 5), so a user may
  turn it off entirely. It *should* warn in the client (§14), not refuse in
  the API.
- Every mutation commits exactly once, at the end of the service method.

### §7.4 Errors — `services/auth/exceptions/__init__.py`

Add a `MfaErrors` class next to the existing `AuthErrors`, following its
style: `@staticmethod`s returning `HTTPException`, one per refusal, each with
a docstring where the status code is a judgement call.

| Method | Status | Detail |
| --- | --- | --- |
| `invalid_code()` | 401 | `"That code is not valid."` |
| `challenge_expired()` | 401 | `"The login attempt expired. Start again."` |
| `credential_not_found()` | 404 | `"MFA method not found"` |
| `already_activated()` | 409 | `"That method is already active."` |
| `activation_not_required()` | 400 | `"This method needs no activation step."` |
| `wrong_kind()` | 400 | `"That method does not take an activation code."` |
| `too_many_credentials()` | 409 | `"You already have the maximum number of MFA methods."` |

On the login path, prefer `AuthErrors.credentials()` over
`MfaErrors.invalid_code()` for a wrong second factor — the two must be
indistinguishable to an unauthenticated caller, and reusing the existing
refusal is how that stays true.

### §7.5 Register the router

`main.py`, alongside the others (`main.py:182-187`):

```python
        self._app.include_router(mfa.router)
```

and add `mfa` to the `from routers import …` line.

### §7.6 `UserDto` gains one field

`services/auth/dtos/user.py`:

```python
    # Whether this account has any activated second factor. Here rather than
    # behind its own call because the client shows a "you have no MFA"
    # warning on every view, and a boolean the session already carries is
    # cheaper than a fetch per navigation. Populated by
    # UserService.get_current_user, like `scopes` above it.
    mfa_enrolled: bool = False
```

`UserService.get_current_user` (`services/user/user_service.py:26`) sets it via
`MfaVerifier(self.db, settings).is_enrolled(user)`. That needs a
`ConfigService` on `UserService`; add it to `__init__` and to `get_with_deps`
as another `Depends(ConfigService.get_with_deps)`.

---

## §8. Admin reset and the CLI break-glass

Requirement 4: an admin, or anyone with database access, can strip every MFA
method from an account whose authenticators are gone.

### §8.1 `DELETE /users/{user_id}/mfa`

`routers/users.py`, alongside the existing lock route:

```python
        self.router.delete("/users/{user_id}/mfa", status_code=204)(self.reset_mfa)
```

`UserService.reset_mfa(user_id)`:

```python
    async def reset_mfa(self, user_id: int) -> None:
        """Remove every MFA method from an account. Requires users:admin.

        Unlike DELETE /users/{id}/lock, this one also requires an interactive
        login. A lock has to be clearable with an API key, because the whole
        point of that design is that a locked-out admin can still act; MFA
        has no such constraint, and an admin key that can strip second
        factors from any account would make MFA optional service-wide for
        whoever steals that key. `python -m reset_mfa` is the break-glass
        path when there is no interactive login to be had.
        """
        self.principal.require_scope(Scopes.USERS_ADMIN)
        self.principal.require_interactive()
        ...
```

Load the user (404 via `UserErrors.not_found()` if absent), call
`MfaCredential.delete_all_for_user`, commit. Idempotent: an account with no
MFA returns 204 too.

### §8.2 `reset_mfa.py` — the CLI

New file at the repository root, next to `unlock_user.py`, `reset_password.py`
and `bootstrap_admin.py`. Copy `unlock_user.py`'s structure exactly: a module
docstring explaining what it is for and showing the invocations, a single
`ResetMfaCommand` class of `@staticmethod`s plus an instance `run(argv)`,
`argparse` with `prog="python -m reset_mfa"`, `int` return codes, and the
`if __name__ == "__main__": raise SystemExit(asyncio.run(...))` block.

```
python -m reset_mfa alice
python -m reset_mfa --list
```

- `--list` prints every user with at least one MFA credential and what kinds
  they hold, and changes nothing.
- The positional form deletes all credentials for that username, prints
  `Removed 2 MFA method(s) from 'alice'.`, or
  `'alice' has no MFA methods; nothing to do.`, and exits 0. An unknown
  username prints to stderr and exits 1 — same shape as
  `UnlockCommand._unlock_user`.
- Use `async with DatabaseService.session() as db:` per operation, as the
  other commands do.
- **It must never decrypt a secret.** It deletes rows and counts them; it has
  no reason to read `MfaCredential.secret` and must not start. That is what
  makes it the recovery path when `MFA_ENCRYPTION_KEYS` is wrong or lost
  (§4.8) — a break-glass tool that needs the key it is recovering from is not
  one. Say so in the module docstring, so the property survives a later edit.
- Update the docstring of `unlock_user.py`? No — but *do* cross-reference from
  this new file's docstring that a user who has lost their authenticator
  usually also needs nothing else, whereas one who is *also* locked out needs
  `python -m unlock_user` as well. `reset_password.py`'s docstring sets that
  precedent.

---

## §9. Replacing the docs Basic prompt

The docs (`/docs`, `/redoc`, `/openapi.json`, `/docs/oauth2-redirect`) are
gated in production only. Today that gate is HTTP Basic. It becomes an HTML
login page with an MFA second step, and a signed cookie.

### §9.1 The invariant this changes, stated plainly

`middleware/client_cors.py`'s docstring and
`services/config/config_service.py`'s `client_allowed_origins` comment both
assert that this API takes **no cookies**. After this change it takes exactly
one, on four routes that are not part of the API surface
(`include_in_schema=False`) and whose cookie grants nothing but the ability to
read the route index.

Both of those comments must be updated to say so, and neither statement they
rest on may change:

- `ClientCorsMiddleware` must continue to send **no**
  `Access-Control-Allow-Credentials`, so cross-origin JavaScript still cannot
  make a credentialed request and read the response.
- `AuthService.authenticate()` must continue to read the bearer token and
  nothing else. The docs cookie is not an API credential and no route outside
  `routers/docs.py` may consult it.

Set the cookie `HttpOnly`, `SameSite=Lax`, `Path=/`, and `Secure` when
`settings.is_production` (which is the only case it is set at all). `Path=/`
rather than `/docs`, because `/openapi.json` and `/redoc` are outside a
`/docs` prefix.

### §9.2 `services/auth/docs_session.py`

```python
class DocsSessionToken:
    """The signed value in the `docs_session` cookie.

    A JWT and not an opaque id for the same reason the MFA challenge is one:
    there is nothing to revoke over an hour, and a stateless value costs the
    documentation no table and no sweep.

    Grants exactly one thing — reading the route index — and is bound to the
    password hash in force when it was issued, so a password change ends
    every docs session against the old one.
    """

    TOKEN_USE: ClassVar[str] = "docs_session"
    COOKIE_NAME: ClassVar[str] = "docs_session"
```

Same claim set as `MfaChallengeToken` minus `ctx`, with
`exp = iat + DOCS_SESSION_MINUTES`. `mint(settings, user)` and
`verify(settings, token) -> int | None`. Reuse the `pwb` computation — put
`password_binding` on a small shared helper class (e.g.
`services/auth/mfa/challenge.py`'s `MfaChallengeToken.password_binding`
called from here, or a `SignedToken` base both extend). Prefer a
`services/auth/signed_token.py` `SignedToken` base holding `_encode`,
`_decode` and `password_binding`, with `MfaChallengeToken` and
`DocsSessionToken` as subclasses — that is the DRY answer and keeps the
`token_use` discipline in one place.

### §9.3 The page renderer

`services/oauth/authorize_page.py`'s `_PAGE` shell is exactly the HTML the
docs login needs. Its rendered output is asserted **byte for byte** by
`tests/test_oauth.py` (see that file's module docstring), so:

1. Create `services/auth/html_page.py`:

   ```python
   class HtmlPage:
       """The minimal page shell shared by the two HTML surfaces this service
       serves: the OAuth authorize form and the docs login.

       Moved out of services/oauth/authorize_page.py rather than copied. The
       authorize page's exact bytes are asserted by tests/test_oauth.py, so
       the template string below must stay character-identical to what that
       file held — this is a relocation, not a redesign.
       """

       TEMPLATE: ClassVar[str] = """..."""  # verbatim from authorize_page.py

       @classmethod
       def render(cls, *, title: str, body: str) -> str:
           return cls.TEMPLATE.format(title=title, body=body)
   ```

2. `AuthorizePageRenderer` drops its own `_PAGE` and calls
   `HtmlPage.render(title=…, body=…)`. Run `uv run pytest tests/test_oauth.py`
   immediately after; it must pass with zero changes to that test file.

3. New `services/auth/docs_login_page.py`:

   ```python
   class DocsLoginPageRenderer:
       """The two forms in front of the production documentation."""

       @classmethod
       def render_login_form(cls, *, next_path: str, error: str | None = None) -> str

       @classmethod
       def render_mfa_form(
           cls, *, next_path: str, mfa_token: str, error: str | None = None
       ) -> str
   ```

   The login form posts to `/docs/login` with `username`, `password` and a
   hidden `next`. The MFA form posts to the same path with a hidden
   `mfa_token`, a hidden `next`, and a visible `code` field
   (`inputmode="numeric"`, `autocomplete="one-time-code"`, `autofocus`).
   Escape everything with `html.escape`, as `AuthorizePageRenderer` does.

### §9.4 The gate

Replace `AuthService.require_docs_access` with a dependency on `DocsRouter`
itself, or a small `DocsAccessGuard` class in `services/auth/docs_session.py`.
Behaviour:

```
outside production                        → pass
production, valid cookie, user still active → pass
otherwise                                  → 303 to /docs/login?next=<current path>
```

The 303 comes from `HTTPException(status_code=303, headers={"Location": …})`.
That returns a JSON body with a redirect status, which is unusual for an API —
acceptable here because these four routes are `include_in_schema=False` and
answered by a browser that follows `Location` regardless of body. Note the
alternative in a comment: a custom exception plus `app.add_exception_handler`
in `main.py` returning a real `RedirectResponse`, which is cleaner and more
wiring. Either is fine; pick one and comment why.

Development still waves everyone through, and for the same reason the current
docstring gives: "a password prompt in front of localhost only trains people
to type one."

### §9.5 The routes

On `DocsRouter`, all `include_in_schema=False`:

- `GET /docs/login` — query param `next`, renders the login form. If already
  holding a valid cookie, 303 straight to `next`.
- `POST /docs/login` — form fields `username`, `password`, `next`, and
  optionally `mfa_token` and `code`.
  - With `mfa_token` present: verify the challenge with
    `MfaChallengeContext.DOCS`, verify the code, on failure re-render
    `render_mfa_form` with an error at status 401 and charge
    `register_mfa_failure`.
  - Otherwise: `authenticate_user(username, password)`; on failure re-render
    `render_login_form` with `"Incorrect username or password."` at 401; on
    success, if the user is MFA-enrolled mint a `DOCS`-context challenge and
    render `render_mfa_form`, else set the cookie and 303 to `next`.
  - `AuthErrors.account_locked` propagating out of `authenticate_user` as a
    429 is fine and correct — do not swallow it.
- `POST /docs/logout` — delete the cookie, 303 to `/docs/login`.

**`next` must be validated.** Only the four known docs paths are acceptable;
anything else falls back to `/docs`. Put the allowlist on `DocsRouter` as a
`ClassVar[frozenset[str]]` built from the existing `OPENAPI_URL`/`DOCS_URL`/
`REDOC_URL`/`OAUTH2_REDIRECT_URL` class vars, and comment that this is an
open-redirect guard, not tidiness.

### §9.6 Test file that must be rewritten

`tests/test_docs.py`'s entire `TestProduction` class assumes Basic auth. It
needs rewriting, not patching:

- `test_asks_the_browser_for_a_password` → assert a 303 to `/docs/login`
  instead of a `WWW-Authenticate` header.
- `test_refuses_an_anonymous_request` → 303, not 401. Use
  `follow_redirects=False` on the httpx call.
- `test_serves_a_user_with_the_right_password` → POST the form, capture the
  `Set-Cookie`, then GET each doc path with it.
- `test_rejects_a_bearer_token` stays meaningful and should stay: a JWT is
  still not a docs credential.
- `test_rejects_a_malformed_header` becomes `test_rejects_a_forged_cookie`.
- `TestDevelopment` is unaffected and must not change.
- Add: an `mfa_pending` token and a `docs_session` token each presented as
  `Authorization: Bearer …` must **not** authenticate (§1.4). These belong in
  `tests/test_auth.py` as well.

---

## §10. The OAuth authorize flow

`services/oauth/oauth_service.py` and `routers/oauth.py`.

### §10.1 Service

`complete_authorize` (`oauth_service.py:199`) currently calls
`authenticate_user` and then issues a code. Two changes:

1. Accept two new parameters, `mfa_token: str | None` and `code: str | None`.
2. Split the "who is this" step:

```python
if mfa_token:
    user = await MfaVerifier(self.db, self.settings).user_from_challenge(
        mfa_token, MfaChallengeContext.OAUTH
    )
    if user is None:
        raise AuthorizeLoginFailed("That login attempt expired. Try again.")
    if not await verifier.verify(user, code or ""):
        await auth_service.register_mfa_failure(user)
        raise AuthorizeMfaRequired(
            MfaChallengeToken.mint(...)[0], "That code is not valid."
        )
else:
    user = await auth_service.authenticate_user(username, password)
    if not user:
        raise AuthorizeLoginFailed("Incorrect username or password.")
    if await verifier.is_enrolled(user):
        raise AuthorizeMfaRequired(
            MfaChallengeToken.mint(self.settings, user, MfaChallengeContext.OAUTH)[0]
        )
```

New exception in `services/oauth/exceptions.py`:

```python
class AuthorizeMfaRequired(Exception):
    """The password was right and a second factor is outstanding.

    Its own exception rather than a variant of AuthorizeLoginFailed because
    the router answers it with a different page — the protocol parameters
    have to survive the extra round trip in hidden fields, and the user must
    not be asked for their password a second time.
    """

    def __init__(self, mfa_token: str, detail: str | None = None): ...
```

Order matters: `register_mfa_failure` may itself raise
`AuthErrors.account_locked` (a 429 `HTTPException`), which will escape the
authorize handler as a plain error response rather than a rendered page. That
is acceptable and honest — note it in a comment rather than catching it.

### §10.2 Renderer

Add `AuthorizePageRenderer.render_mfa_form(...)` — **a new method**. Do not
modify `render_authorize_form`; `tests/test_oauth.py` asserts its exact
output. The new form carries every hidden field the existing one does, plus
`mfa_token`, drops `username`/`password`, and adds a `code` field. Keep the
Approve/Deny buttons so a user can still back out at the second step.

### §10.3 Router

`authorize_submit` (`routers/oauth.py:166`) gains `mfa_token` and `code` form
fields, both defaulting to `""` for the same reason every other field there
does (Starlette treats a submitted-empty value as absent, and FastAPI would
422 before the handler runs — the existing comment explains this). Catch
`AuthorizeMfaRequired` and return `HTMLResponse(render_mfa_form(...))` at
status 200 for the first challenge and 401 for a rejected code.

---

## §11. Configuration

`services/config/config_service.py`, a new block after the login-throttling
one, in that block's commented style:

```python
# --- Multi-factor authentication ---------------------------------------
# MFA is per-account and opt-in, so there is deliberately no global
# on/off switch: a setting that silently stops demanding a second factor
# from accounts that enrolled one is a footgun, not a feature. What is
# configurable is the shape of the challenge, not whether it happens.

# How long the token returned by the first step stays redeemable. Long
# enough to find a phone, short enough that one left in a shell history
# is worthless.
mfa_challenge_ttl_minutes: int = Field(
    default=5, ge=1, alias="MFA_CHALLENGE_TTL_MINUTES"
)
# Thirty-second steps either side of now that a TOTP code is accepted
# for. 1 tolerates roughly a minute and a half of clock skew between a
# phone and this server, which is the usual recommendation; 0 demands
# perfectly synchronised clocks and will generate support requests.
mfa_totp_drift_steps: int = Field(default=1, ge=0, alias="MFA_TOTP_DRIFT_STEPS")
mfa_backup_code_count: int = Field(default=10, ge=1, alias="MFA_BACKUP_CODE_COUNT")
# The issuer an authenticator app shows beside the account name. Purely
# cosmetic, and worth setting when one person runs more than one of these.
mfa_issuer: str = Field(default="resume-api", alias="MFA_ISSUER")

# Fernet keys for the TOTP secrets at rest, newest first: every key
# listed can decrypt, the first one encrypts. Rotating is therefore
# prepending a new key and leaving the old one until the lazy re-seal in
# TotpMethod.verify has worked through the enrolled accounts.
#
# Deliver this through SECRETS_DIR in production, not an environment
# variable — `docker inspect` reads env vars, and this key is the whole
# of what stands between a database dump and everyone's second factor.
#
# NoDecode for the same reason the two frozensets above carry it:
# pydantic-settings tries to JSON-decode any complex annotation before a
# validator sees it, so a plain comma-separated string never reaches
# `_parse_mfa_encryption_keys`.
mfa_encryption_keys: Annotated[list[SecretStr], NoDecode] = Field(
    default_factory=list, alias="MFA_ENCRYPTION_KEYS"
)

# How long a documentation login lasts. The docs are read, not acted on,
# so this is a browsing session rather than a credential lifetime.
docs_session_minutes: int = Field(default=60, ge=1, alias="DOCS_SESSION_MINUTES")
```

Two validators, alongside the existing ones:

```python
@field_validator("mfa_encryption_keys", mode="before")
@classmethod
def _parse_mfa_encryption_keys(cls, value):
    if not isinstance(value, str):
        return value
    return [key.strip() for key in value.split(",") if key.strip()]


@field_validator("mfa_encryption_keys", mode="after")
@classmethod
def _validate_mfa_encryption_keys(cls, value):
    """Reject key material Fernet cannot use, at settings load.

    A malformed key is otherwise a 500 on the first enrolment and a
    lockout on every login after it. Fernet wants exactly 32 bytes,
    url-safe base64 encoded; anything else fails here, where the message
    can say so.
    """
    for key in value:
        try:
            Fernet(key.get_secret_value().encode())
        except (ValueError, TypeError) as error:
            raise ValueError(
                "MFA_ENCRYPTION_KEYS entries must be url-safe base64 "
                "encoded 32-byte keys. Generate one with: python -c "
                '"from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            ) from error
    return value
```

And a `model_validator(mode="after")`, next to
`_require_public_base_url_in_production` and following its shape exactly:

```python
    @model_validator(mode="after")
    def _require_mfa_encryption_key_in_production(self) -> ConfigServiceModel:
        """Plaintext TOTP secrets are a development affordance, not a
        deployment option. Locally there is nothing worth encrypting and a
        required key would only be ceremony before `uv run pytest`; in
        production the absence of one is a configuration mistake nobody
        notices until a database leaks."""
        if self.is_production and not self.mfa_encryption_keys:
            raise ValueError(
                "MFA_ENCRYPTION_KEYS is required in production: TOTP secrets "
                "are symmetric, and storing them in the clear makes a database "
                "read sufficient to mint second factors."
            )
        return self
```

Note the import order consequence: `from cryptography.fernet import Fernet` at
the top of `config_service.py` is the first time this module imports anything
outside pydantic and the standard library. That is fine, but if the validator
feels like the wrong home for it, put the check on
`MfaSecretBox.validate_key(...)` and call that from here instead — the
settings module then depends on `services.auth.mfa`, which is a heavier
coupling. Either is defensible; take the local import.

Also add every one of these to:

- `.env.example` — in the same grouped, blank-line-separated style as the
  `AUTH_LOCKOUT_*` block, with the defaults spelled out. `MFA_ENCRYPTION_KEYS`
  gets a commented-out example plus the generation command, and a line saying
  it is required in production and belongs in `SECRETS_DIR` there.
- `docs/configuration.md` — a new `## Auth — multi-factor` section between
  "Auth — login throttling" and "Bootstrap admin", as a three-column table
  matching the others, plus a `DOCS_SESSION_MINUTES` row added to the
  "Auth — tokens" table. The `MFA_ENCRYPTION_KEYS` row is the one that needs
  real prose rather than a phrase: how to generate a key, that it is required
  in production, that rotation is prepend-and-wait, that losing it locks out
  every enrolled account, and that it must be backed up somewhere other than
  beside the database dump. Follow `AUTH_TRUSTED_PROXY_HOPS`'s row as the
  precedent for how long a cell in that table is allowed to be.

---

## §12. Tests

`CLAUDE.md` requires over 90% coverage; `.github/workflows/test.yml` runs the
suite. Run `uv run pytest --cov` and check the new modules specifically.

### §12.1 New files

**`tests/test_mfa.py`** — the methods and the registry, mostly against
in-memory rows, following `tests/test_lockout.py`'s stated approach of
exercising policy objects directly and the wiring through HTTP.

- `TotpMethod.verify` accepts a code generated by `pyotp` for the current step.
- Accepts `-1` and `+1` drift; rejects `-2` and `+2` at the default setting.
- Rejects a code already accepted (the replay guard), including one presented
  twice inside the same step.
- Rejects a code whose step is *earlier* than `last_used_step`.
- Correct under a non-UTC process timezone (`TZ` + `time.tzset()`) — see §4.3.
- Spaces in the entered code are tolerated.
- `complete_enrollment` activates on a good code, refuses on a bad one, and
  refuses a credential already active.
- `BackupCodesMethod`: a generated code verifies once and never again;
  formatting separators are tolerated; `remaining` counts down; regenerating
  destroys the previous set.
- `MfaMethodRegistry.for_credential` returns None for an unknown kind and the
  right class for each known one.
- `MfaVerifier.required_kinds` ignores credentials with `activated_at IS NULL`.

**`tests/test_mfa_secret_box.py`** — §4.8, mostly against the class directly.

- Round trip: `open(seal(x)) == x`, and `seal` output carries the prefix.
- Two seals of the same plaintext differ (Fernet's IV), and both open.
- A tampered ciphertext returns `None` rather than raising — assert with
  `pytest.raises` *not* firing, since the point is that it does not.
- A key not in the configured list returns `None`.
- Rotation: seal under key B, then configure `[A, B]` — `open` still works,
  `reseal` produces a token that opens under `[A]` alone.
- `reseal` on a value it cannot open returns the input unchanged. This is the
  "a failure here must not destroy a secret" property and deserves a comment
  saying so.
- Null box (no keys): `seal` is identity, `open` is identity, `is_sealed` is
  False. Then configure a key and confirm an unprefixed legacy value still
  opens — the "added a key later" path.
- `ConfigServiceModel` rejects a malformed `MFA_ENCRYPTION_KEYS` entry, and
  refuses to build in production with none set. These go in
  `tests/test_docs.py`'s `TestEnvironmentSetting` class or a new settings
  test — wherever `_require_public_base_url_in_production` is covered today;
  find it and sit beside it.
- `StartupTasks.verify_mfa_key` is silent with no sealed rows, silent with a
  matching key, and raises with a mismatched one. `tests/test_startup.py` is
  the home for this.
- **The plaintext secret never reaches the column.** Enrol through the API,
  then read `mfa_credentials.secret` directly and assert the base32 seed does
  not appear in it. This is the test that would actually catch someone
  removing the seal call.

**`tests/test_mfa_login.py`** — the two-step `/token` flow through HTTP.

- A user with no MFA still gets a token in one step (regression guard for
  §1.3).
- An enrolled user gets `mfa_required`, and the payload has no `access_token`.
- The challenge redeems with a valid TOTP code and with a backup code.
- A wrong code is 401 **and** increments the account's `failed_login_count`.
- Enough wrong codes lock the account (use `test_lockout.py`'s `quick_lockout`
  fixture pattern).
- An expired challenge is 401 (freeze or shift `exp` by minting one by hand).
- A challenge minted with `ctx="docs"` is refused at `/token/mfa`.
- A challenge is refused after the password changes (the `pwb` binding).
- **An `mfa_pending` token used as `Authorization: Bearer …` does not
  authenticate.** Likewise `docs_session`. Put these in `tests/test_auth.py`
  next to the existing "every way a JWT can fail to identify someone" cases.
- A deactivated user's outstanding challenge is refused.

**`tests/test_mfa_management.py`** — the `/users/me/mfa` routes.

- Enrol → activate → `GET` shows it active and `mfa_enrolled` is true on
  `/users/me`.
- An unactivated credential does not make login demand a second factor.
- Wrong `current_password` refuses every mutating route, with the throttle
  charged.
- An API key (not interactive) is refused on every route, `GET` included.
- Removing a credential belonging to another user is a 404, not a 403.
- `GET` never returns a secret, `otpauth_uri`, or a backup code — assert on
  the raw response text, not the parsed model.
- Removing the last method succeeds and turns `mfa_enrolled` back to false.
- The per-user credential cap refuses the eleventh.

**`tests/test_reset_mfa.py`** — mirror `tests/test_reset_password.py`
exactly: invoke `ResetMfaCommand().run([...])` directly, assert the return
code and the database state, and cover `--list`, an unknown username, and an
account with nothing to remove.

### §12.2 Files that change

- **`tests/test_docs.py`** — rewritten `TestProduction`, per §9.6.
- **`tests/test_oauth.py`** — add MFA cases (challenge page rendered, code
  accepted, code rejected, deny at the second step). The existing byte-for-byte
  assertions on `render_authorize_form` must pass **unchanged** after the
  `HtmlPage` extraction; treat any change there as a bug in the extraction.
- **`tests/test_auth.py`** — the two bearer-token rejection cases above.
- **`tests/test_users.py`** — `DELETE /users/{id}/mfa`: admin succeeds,
  non-admin is 403, an API key is 403 (interactive required), unknown user is
  404, an account with no MFA is still 204.
- **`tests/conftest.py`** — three additions, nothing removed.

  First, at module scope with the other pinned environment variables (above
  the `import pytest` line, for the reason that file's docstring gives):

  ```python
  from cryptography.fernet import Fernet  # with the stdlib imports at the top

  # Generated per run, like AUTH_SECRET_KEY above: the suite encrypts TOTP
  # secrets the same way a deployment does, so a change that stops sealing
  # them fails here rather than in production.
  os.environ["MFA_ENCRYPTION_KEYS"] = Fernet.generate_key().decode()
  ```

  This is the one import that has to sit above the `import pytest` block
  rather than with the application imports below it, because the environment
  variable it produces must be set before `services.config` is first read —
  the same constraint the file's docstring already describes.

  Then the two fixtures:

  ```python
  @pytest.fixture
  async def enrolled(make_actor):
      """An actor with an activated TOTP credential, plus its secret."""
      # returns something like (Actor, secret) or a small dataclass


  @pytest.fixture
  def totp_code():
      """Current code for a secret, as an authenticator app would show it."""
      return lambda secret: pyotp.TOTP(secret).now()
  ```

  Do **not** change `make_actor`, `admin`, `member`, `roleless` or
  `other_owner` — every existing test depends on those logging in in one step.

---

## §13. Documentation (API repo)

- **`README.md`**
  - New `### Second factors` subsection under `## Authorization model`,
    after `### Two credential types`, covering: opt-in per account; the two
    methods; the two-step `/token` shape with a `curl` example matching the
    style already used in "Pointing an MCP client at a key"; and that a wrong
    code costs the same as a wrong password against the throttle.
  - Within it, a short paragraph on secrets at rest, in the frank register
    the rest of that file uses. It must claim what §0.1 claims and not more:
    TOTP seeds are Fernet-encrypted under `MFA_ENCRYPTION_KEYS`, so a
    database read alone no longer mints second factors — and the key is in
    the application process, so this is not a defence against a compromised
    host. Say that backup codes are hashed rather than encrypted, and that
    this is why they still work if the key is lost.
  - Recovery paragraph: `DELETE /users/{id}/mfa` for an admin,
    `python -m reset_mfa <username>` when there is no admin to be had (and
    that it needs no encryption key, because it deletes rather than reads),
    and the fact that backup codes exist precisely so neither is usually
    needed.
  - One deployment sentence, because it is the mistake this design invites:
    **back the encryption key up somewhere other than beside the database
    dump.** A backup containing both is a backup with no encryption.
  - `### Failed logins are throttled` — amend "Both `/token` and the docs
    login go through the same `authenticate_user`" to include the MFA step and
    the OAuth form.
  - `### The docs are not public in production` — rewrite. Basic is gone; it
    is a login page, an MFA step, and an hour-long `HttpOnly` cookie now. Keep
    the existing argument for *why* the docs are gated at all.
  - `## Layout` — add `reset_mfa.py` is a root script like the others, and
    mention `services/auth/mfa/` on the `services/auth/` line.
- **`docs/configuration.md`** — §11.
- **`docs/oauth-setup.md`** — one paragraph: a connector authorizing against
  an MFA-enrolled account sees a second page.
- **`docs/local-only.md`** — check whether it walks through the docs login;
  if it mentions the Basic prompt, update it.
- **`.env.example`** — §11.

---

## §14. Browser client (`resume-mcp-api-clients`)

All in `web/index.html`, one file, no build step. Match its existing style:
`htm` tagged templates, hooks, the global `store`, `class`-free function
components, and comments that explain *why* rather than *what*.

### §14.1 The QR module

Add a third `<link rel="modulepreload">` beside the two at `index.html:17-18`.
`web/build.sh` discovers these tags with a regex (`build.sh:62`) and vendors
whatever it finds, so **no build script change is needed** — but its comments
say "the two remote imports" in two places (`build.sh:79`, `build.sh:91`);
update the wording.

Steps, in order:

1. Pick a small QR generator that jsdelivr can serve as an ES module. The
   `/+esm` endpoint transpiles CJS packages, so
   `https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/+esm` is the leading
   candidate. **Verify before hardcoding**: fetch it and confirm it is a
   module with a usable default export.
2. Compute the SRI hash with the exact command `build.sh` uses to check it,
   so the two can never disagree:

   ```bash
   curl -fsSL "<url>" | openssl dgst -sha384 -binary | openssl base64 -A
   ```

3. Add the tag with `integrity="sha384-<that>"` and
   `crossorigin="anonymous"`, pinned to an exact version — the comment at
   `index.html:5-16` explains why the preload tag is what carries integrity
   for a bare `import`, and says never to use `@latest`. Honour that.
4. Render into an inline `<svg>` or a data URI. Do not inject unsanitised
   markup; if the library only emits an HTML string, set it on a
   `ref`'d element in a `useEffect` and note in a comment that the input is
   an `otpauth://` URI the server just produced, not user text.

**Fallback if step 1 fails.** If no suitable ESM build exists, ship the
enrolment card without a QR: show the base32 secret in a monospace field with
the existing `copyText` helper (`index.html:615`), plus the `otpauth://` URI
as a clickable link (mobile authenticators register that scheme). Note the
omission in `web/README.md`. Do not add a build step to work around it.

### §14.2 Login: the second step

`LoginView` (`index.html:951`) and `api.login` (`index.html:733`).

- `api.login` returns whatever `/token` gave. Add
  `api.completeMfa(apiBase, mfaToken, code)` posting form-encoded to
  `/token/mfa`.
- `LoginView` gains a `pending` state holding `{mfaToken, methods}`. When
  `submit`'s response has `mfa_required`, set it instead of saving a session;
  the form then renders a single code field (`inputmode="numeric"`,
  `autocomplete="one-time-code"`, `autofocus`) plus a "Back" link that clears
  `pending`.
- Everything after a successful second step is the code that already follows a
  successful login: `decodeJwtExp`, `saveSession`, `store.set`, then
  `Promise.all([api.me(), api.schemas()])`. Extract it into one local helper
  rather than duplicating it in both branches.
- Show a hint when `methods` includes `backup_codes`: "Lost your
  authenticator? A backup code works here too."
- A 401 from `/token/mfa` shows the error and keeps the user on the code
  screen; the `request()` helper's session-clearing 401 branch does not apply
  because `auth: false` is set on both login calls (`index.html:701` gates on
  `auth`).

### §14.3 The "no MFA" warning

Requirement 5. In `App` (`index.html:2365`), between `<${Header} />` and
`<main>`:

```js
${user && !user.mfa_enrolled && html`
  <div class="banner warn" style="margin: var(--space-3) var(--space-5) 0">
    Your account has no second factor. <a href="#" onClick=${goToAccount}>Set one up</a>.
  </div>
`}
```

Dismissible for the session only (a `useState` in `App`, deliberately not
persisted — a nag that can be silenced forever is not a warning). Use the
existing `.banner.warn` class; no new CSS is needed. `user.mfa_enrolled` comes
from `/users/me` (§7.6), so no extra request.

### §14.4 MFA management UI

Requirement 6. Add a `Security` entry to `NAV_ITEMS` (`index.html:1043`)
between "API keys" and "Account", a `security` branch in `CurrentView`
(`index.html:2349`), and a `SecurityView`. Putting it in its own view rather
than inside `AccountView` keeps the password form uncluttered and gives the
warning banner somewhere specific to link to.

`SecurityView` contains:

1. **Status list.** A `table.reflow` (the existing responsive pattern —
   see `index.html:1137` and the CSS at `index.html:441`) of enrolled
   methods: label, kind badge, added date, last used, remaining (for backup
   codes), and a Remove button. Reuse the `.badge` classes. Empty state uses
   `.empty-state`.
2. **Add an authenticator app.** A form taking a label, then on submit a modal
   showing the QR, the secret as selectable monospace text with a Copy button
   (`copyText`, `index.html:615`), and a code field to activate. Model the
   modal on `ApiKeyRevealModal` (`index.html:1890`), including its
   "I have saved this" gate — same one-time-secret problem, same solution.
   The modal must not be dismissible without either activating or explicitly
   cancelling (cancelling should call `DELETE` on the pending credential so
   half-enrolled rows do not accumulate).
3. **Backup codes.** A button that generates (or regenerates) a set, and a
   modal listing them in a monospace grid with Copy-all and Print. Regenerating
   warns that the previous set stops working — `RenameDialog`
   (`index.html:1422`) is the precedent for a dialog that names consequences.
4. **Current-password confirmation.** Enrolment, regeneration and removal all
   require it (§7.2). Use one small `ConfirmPasswordDialog` component for all
   three rather than three password fields. Include the off-screen
   `username` input with `autocomplete="username"` that `AccountView` uses
   (`index.html:2273`) — the comment there explains why `display:none` is
   wrong and `.visually-hidden` is right.
5. **A warning when removing the last method**, in the removal dialog: "This
   is your only second factor. Removing it means your password alone will log
   you in." Warn, do not block — the API does not block either.

### §14.5 `api` object additions

Next to the existing entries at `index.html:732`:

```js
    mfaStatus: () => request("GET", "/users/me/mfa"),
    enrollTotp: (body) => request("POST", "/users/me/mfa/totp", { body }),
    activateTotp: (id, body) =>
      request("POST", `/users/me/mfa/totp/${id}/activate`, { body }),
    regenerateBackupCodes: (body) =>
      request("POST", "/users/me/mfa/backup-codes", { body }),
    removeMfa: (id, body) => request("DELETE", `/users/me/mfa/${id}`, { body }),
```

`request` already sends a body on any method (`index.html:672`), so `DELETE`
with a body needs no helper change.

### §14.6 Client documentation

`web/README.md`:

- "What's here" is unchanged (still one file).
- Add MFA to the feature description near the top.
- The **Mobile** section already ranks tasks for a phone; add that entering a
  TOTP code and reading backup codes are now among them, and that
  `autocomplete="one-time-code"` lets iOS and Android offer the code from the
  SMS/clipboard picker.
- The CDN list in the intro comment and the build.sh description both say
  "two" modules; update to three.

`resume-mcp-api`'s own `README.md` `## Browser client` section does not
enumerate the client's features, so it needs no change beyond §13.

---

## §15. Order of work

Each step should leave the suite green.

1. **§2** dependency, `uv lock`.
2. **§3** models, `env.py` registration, migration. Run
   `uv run alembic upgrade head` against a scratch SQLite file and confirm
   both tables and both indexes appear.
3. **§5.1** the `token_use` guard in `_authenticate_jwt`, with its test. Do
   this before anything mints a new token kind.
4. **§4.8 + the §11 settings it needs** — `MfaSecretBox`, the two validators,
   the production model validator, the `conftest.py` key, and
   `tests/test_mfa_secret_box.py`. Ahead of `TotpMethod`, so there is never a
   commit in the history that writes a plaintext secret to the column.
   `StartupTasks.verify_mfa_key` can come with it or with step 11.
5. **§4** the rest of the `mfa` package, with `tests/test_mfa.py`. No HTTP yet.
6. **§5.2–§5.4** `AuthService` changes. `tests/test_users.py` and
   `tests/test_lockout.py` must pass unchanged.
7. **§6** `/token` and `/token/mfa`, with `tests/test_mfa_login.py`.
8. **§7** management routes, with `tests/test_mfa_management.py`.
9. **§8** admin route and CLI, with `tests/test_reset_mfa.py`.
10. **§9** docs login. The `HtmlPage` extraction first, confirming
    `tests/test_oauth.py` still passes, *then* the new pages and routes, then
    the `tests/test_docs.py` rewrite.
11. **§10** OAuth second step.
12. **§11** the remaining settings, `.env.example`, `docs/configuration.md`.
    The encryption settings landed in step 4; this is the rest of the block.
13. **§13** README and remaining docs.
14. **§14** the client, in the other repo, once the API is running locally
    and can be pointed at.

### Before committing

The repo's own checklist (`README.md` "Before committing") plus
`.pre-commit-config.yaml`:

```bash
uv run pytest --cov
```

```bash
uv run ruff check . && uv run ruff format --check .
```

```bash
uv run ty check
```

```bash
uv run bandit -c pyproject.toml -r .
```

Bandit will flag the generated secrets and the token comparisons; if any is a
false positive, `# nosec <ID>` with a reason on the line, as `routers/auth.py`
already does for `token_type="bearer"`. Do not lower the bandit thresholds in
`pyproject.toml`.

Two commits, one per repository. The API commit should mention that
`clients/` (the submodule pointer) is bumped only after the client repo's
commit exists.

---

## §16. Things that are explicitly out of scope

- SMS and email factors. The `MfaMethod`/`MfaMethodRegistry` seam exists for
  them (§4), and adding one should touch a new module in `methods/` and one
  line in `registry.py` and nothing else — but no delivery mechanism,
  rate-limit budget, or provider integration is designed here.
- WebAuthn / passkeys. A genuinely different shape (a challenge-response
  ceremony, not a code), and it would not fit the `verify(credential, code)`
  interface. Worth its own design.
- "Remember this device". Needs a device table, which is state, which is what
  §0 decided against.
- Requiring MFA by policy (per-role or service-wide). Requirement 5 says MFA
  is optional; a mandatory mode is a separate feature with its own migration
  story for accounts that have not enrolled.
- A managed KMS or envelope encryption for the TOTP key. §4.8 keeps the key
  in the process, delivered through `SECRETS_DIR`. Moving to KMS or Secret
  Manager would change `MfaSecretBox.from_settings` and nothing else, which
  is the point of putting it behind that class — but it brings a cloud
  dependency and an availability question this deployment has not needed.
- Scheduled key rotation. `MultiFernet` plus the lazy re-seal in
  `TotpMethod.verify` makes rotation *possible* (prepend a key, wait, drop
  the old one); nothing here automates it or reports how far it has got.
