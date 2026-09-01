# Configuration reference

Every setting this service reads, grouped by concern. `.env.example` is the
copy-and-fill-in version of this file; this page is the same information
restructured for lookup.

Configuration is read once per process — changing a value needs a restart.

**"Default" means the code's fallback**, applied when the setting is absent
entirely. Settings marked required have none: the service refuses to start
without them. `.env.example` ships a working value for every one of those, so a
copied `.env` starts as-is — but deleting one of those lines is not the same as
accepting a default, and the value in `.env.example` is noted where it differs
from nothing at all.

## Environment

| Setting | Default | |
| --- | --- | --- |
| `ENVIRONMENT` | `development` | `development` or `production`. Development leaves `/docs`, `/redoc` and `/openapi.json` open. Production puts all three behind an HTML login (and, for an MFA-enrolled account, a code) — see the README's "The docs are not public in production". Nothing else branches on this. |

## Database

| Setting | Default | |
| --- | --- | --- |
| `DATABASE_URL` | — (required; `.env.example` sets `sqlite+aiosqlite:///data/app.db`) | SQLite needs nothing else. For Postgres: `postgresql+asyncpg://USER:PASSWORD@HOST/resume_api?ssl=require` — `alembic/env.py` rewrites this to `postgresql+psycopg` for migrations, translating `ssl` to libpq's `sslmode` on the way, so one setting drives both engines. |
| `DATABASE_ECHO` | `false` | Logs every statement with bind parameters. Leave off outside local debugging — user writes carry password hashes. |
| `RUN_MIGRATIONS_ON_STARTUP` | `true` | Runs `alembic upgrade head` before the first request. On by default so a local run needs nothing extra. Turn off where a separate pipeline step migrates instead — on a scale-to-zero host this would otherwise run on every cold start, and parallel starts would race for the same lock. With it off, startup fails rather than serving against a schema it does not expect. |

## Auth — tokens

| Setting | Default | |
| --- | --- | --- |
| `AUTH_SECRET_KEY` | — (required) | Signs access tokens, MFA challenges and the docs-session cookie — all three are JWTs distinguished by a `token_use` claim. Generate a fresh one per deployment: `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `AUTH_ALGORITHM` | — (required; `.env.example` sets `HS256`) | |
| `AUTH_ACCESS_TOKEN_EXPIRE_MINUTES` | — (required; `.env.example` sets `30`) | How long a `/token` access token lasts. |
| `DOCS_SESSION_MINUTES` | `60` | How long a documentation login lasts. The docs are read, not acted on, so this is a browsing session rather than a credential lifetime — see "Auth — multi-factor" below and the README's "The docs are not public in production". |

## Auth — login throttling

Failed password logins are counted, per account and per calling address.
Every value here has a working default; the whole block can be deleted.
Applies to `/token`, the docs login, and `POST /oauth/authorize`, which all
share one code path, and to a wrong MFA code at any of their second steps —
API keys are not throttled, since they are 32 random bytes with no
dictionary to attack.

| Setting | Default | |
| --- | --- | --- |
| `AUTH_LOCKOUT_ENABLED` | `true` | Off disables both counters. Meant for the test suite, not a deployment — a lockout switched off locally is a lockout nobody notices is broken. |
| `AUTH_LOCKOUT_MAX_ATTEMPTS` | `5` | Failed attempts within the window that lock an account. |
| `AUTH_LOCKOUT_WINDOW_MINUTES` | `15` | How far back that window reaches. Fixed, not sliding — a patient attacker retains a sustained four-guesses-per-window at these values. |
| `AUTH_LOCKOUT_BASE_MINUTES` | `15` | How long the first lock lasts. Each further lock without a successful login in between doubles it: 15, then 30, then 60. |
| `AUTH_LOCKOUT_PERMANENT_AFTER_LOCKS` | `4` | Which lock stops being temporary. After this many, an admin has to run `DELETE /users/{id}/lock` or `python -m admin_cli unlock`. A successful login resets the ladder, so an account in daily use cannot be walked up to this by an outsider. A lock never blocks an API key, which is what keeps a locked-out admin able to unlock themselves. |
| `AUTH_IP_MAX_FAILURES` | `20` | Same idea, keyed on the caller's address — catches one guess each across a list of usernames, where no single account sees enough failures to lock. |
| `AUTH_IP_WINDOW_MINUTES` | `15` | |
| `AUTH_IP_BAN_MINUTES` | `15` | Bans here are always temporary; an address is a lease, not an identity. |
| `AUTH_TRUSTED_PROXY_HOPS` | `1` | How many proxies sit in front of this process, counted from the **right** of `X-Forwarded-For` — not the leftmost entry, since a client can send that header itself and a proxy appends rather than replaces it. `1` is right for a single reverse proxy in front with no load balancer behind it; add one per additional proxy. `0` turns address throttling off, which is the honest setting when nothing trustworthy sets the header — including a service exposed directly, where the header cannot be believed at all. Getting this wrong throttles every caller as the proxy. |

## Auth — multi-factor

MFA is per-account and opt-in — there is no setting that turns it on or off
service-wide, only ones that shape the challenge once an account has
enrolled a second factor. See the README's "Second factors" for the full
picture; this table is the settings alone.

| Setting | Default | |
| --- | --- | --- |
| `MFA_CHALLENGE_TTL_MINUTES` | `5` | How long the token `/token`, the docs login, or `POST /oauth/authorize` returns after a correct password stays redeemable with a code. Long enough to find a phone, short enough that one left in a shell history is worthless. |
| `MFA_TOTP_DRIFT_STEPS` | `1` | Thirty-second steps either side of now that a TOTP code is accepted for. `1` tolerates roughly ninety seconds of clock skew between a phone and this server; `0` demands perfectly synchronised clocks and will generate support requests. |
| `MFA_BACKUP_CODE_COUNT` | `10` | How many single-use recovery codes a set contains. Regenerating replaces the set outright — there is only ever one live. |
| `MFA_ISSUER` | `resume-api` | The issuer name shown beside the account in an authenticator app. Purely cosmetic; worth setting when one person runs more than one deployment of this service. |
| `MFA_ENCRYPTION_KEYS` | — (required in production) | Fernet keys sealing TOTP secrets at rest, comma-separated, newest first. See below. |

### `MFA_ENCRYPTION_KEYS`

Every key listed can decrypt; the first one encrypts. Generate one with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Unset locally, where a development run stores TOTP secrets in the clear.
Startup refuses to begin in production without at least one, because a
plaintext seed is symmetric — whoever reads the row can mint valid codes for
that account forever.

**Rotation is prepend-and-wait.** Add a new key ahead of the old one;
`TotpMethod.verify` re-seals each credential under it the next time that owner
logs in, so the old key can be dropped once enough time has passed.

**Losing every configured key locks every enrolled account out of password
login.** Startup's `verify_mfa_key` catches a key that does not match what is
stored and refuses to start, rather than letting that surface as everyone's
login breaking at once. When a key is genuinely gone, `python -m admin_cli
reset-mfa` deletes MFA rows without decrypting them, so it works even when
this setting is the thing that is wrong.

Deliver it through `SECRETS_DIR`, not a bare environment variable — `docker
inspect` reads env vars, and this key is the whole of what stands between a
database dump and every account's second factor. **Back it up somewhere other
than beside that dump**; a backup holding both is a backup with no encryption.

## Bootstrap admin (first start only)

Migrations seed no users. On a database with no admin, and only then, these
create one; afterwards they are ignored, so they can stay set in a compose
file without recreating anything on restart. Leave unset to create the admin
by hand instead: `python -m admin_cli bootstrap-admin`. Either way, the new
admin is seeded with the same three example documents as any other new
account — see the README's "Documents".

| Setting | Default | |
| --- | --- | --- |
| `BOOTSTRAP_ADMIN_USERNAME` | — | |
| `BOOTSTRAP_ADMIN_PASSWORD` | — | Must satisfy the same rules the API enforces: 8+ characters with upper, lower, digit and symbol. Startup fails if these are set but cannot produce an admin. |
| `BOOTSTRAP_ADMIN_EMAIL` | — | |

## OAuth 2.1 (MCP connectors)

See [oauth-setup.md](oauth-setup.md) for the full walkthrough.

| Setting | Default | |
| --- | --- | --- |
| `PUBLIC_BASE_URL` | `http://localhost:8000` outside production | The service's own issuer/resource URL. Every OAuth token is checked against it, so a wrong value in production rejects them all with no useful error — set it explicitly there. |
| `OAUTH_ALLOWED_REDIRECT_HOSTS` | — (required; `.env.example` sets `claude.ai`) | Hosts a Dynamic-Client-Registration request's `redirect_uri` may target. An empty value is rejected at startup rather than treated as an empty allowlist, since that would silently block every registration. Comma-separated, any number of entries, one leading `*.` wildcard per entry. `claude.ai` covers Claude web, Desktop, mobile and Cowork; Claude Code's loopback redirect is allowed by a built-in rule and must not be listed here. |
| `OAUTH_REGISTRATION_ENABLED` | `false` | Whether `POST /oauth/register` accepts anonymous Dynamic Client Registration. Off by default: the endpoint cannot require a credential, so leaving it on is an unauthenticated database write anyone who finds it can repeat. Closed, it 404s and the metadata document omits `registration_endpoint` entirely. Register clients with `POST /oauth-clients` instead (requires `users:admin` and an interactive login) — both Claude Code (`--client-id`) and claude.ai (Advanced settings) accept the resulting credentials. |

## Browser client

The client is a separate repository and optional; this API has no build-time
dependency on one. See the README's "Browser client".

| Setting | Default | |
| --- | --- | --- |
| `CLIENT_ALLOWED_ORIGINS` | empty | Origins allowed to call the authenticated routes (`/token`, `/documents`, `/api-keys`, `/users/*`) from a browser. Comma-separated, empty by default — no browser can read these responses until set. Serving the client over a separate static server needs exactly one entry here. |
| `CLIENT_HTML_PATH` | unset | Serve a client same-origin instead: set this and `GET /client` returns that file. Same-origin means `CLIENT_ALLOWED_ORIGINS` does not even need to name it. Unset by default, so the API depends on no build artifact. In an image, this is wherever the build context put the file before `COPY . /app`. |

**On adding `null` to `CLIENT_ALLOWED_ORIGINS`.** Add it only to open a client
straight off disk, and prefer a `http://localhost:PORT` origin if you have the
choice. `null` is not just "the `file://` origin" — it
is what *any* website gets by putting a request inside a sandboxed iframe, so
unlike every other entry here it names no one: allowlisting it lets an
arbitrary page read responses from this API using the browser of whoever
visits it. What that is worth to an attacker is bounded — there are no
cookies, and a token lives in the real client origin's `localStorage`, which a
null-origin frame cannot reach — but it does give away the unauthenticated
surface, chiefly whether a `/token` login succeeded, which turns any visitor's
browser into a credential-testing proxy against this service. The per-address
lockout throttles that; it does not remove it. Local use only — a production
deployment should not set this.

## Docker

| Setting | Default | |
| --- | --- | --- |
| `SECRETS_DIR` | unset | Point this at a secrets mount and any setting above can be supplied as a file instead of an environment variable — one file per lowercased name, e.g. `/run/secrets/bootstrap_admin_password`. Preferred for the two bootstrap-admin secrets, since environment variables are visible in `docker inspect`. |
