# Configuration reference

Every setting this service reads, grouped by concern. `.env.example` is the
copy-and-fill-in version of this file; this page is the same information
restructured for lookup.

Configuration is read once per process — changing a value needs a restart.

## Environment

| Setting | Default | |
| --- | --- | --- |
| `ENVIRONMENT` | `development` | `development` or `production`. Development leaves `/docs`, `/redoc` and `/openapi.json` open. Production puts all three behind HTTP Basic, checked against the users table — see the README's "The docs are not public in production". Nothing else branches on this. |

## Database

| Setting | Default | |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite+aiosqlite:///data/app.db` | SQLite needs nothing else. For Postgres: `postgresql+asyncpg://USER:PASSWORD@HOST/resume_api?ssl=require` — `alembic/env.py` rewrites this to `postgresql+psycopg` for migrations, translating `ssl` to libpq's `sslmode` on the way, so one setting drives both engines. |
| `DATABASE_ECHO` | `false` | Logs every statement with bind parameters. Leave off outside local debugging — user writes carry password hashes. |
| `RUN_MIGRATIONS_ON_STARTUP` | `true` | Runs `alembic upgrade head` before the first request. On by default so a local run needs nothing extra. Turn off where a separate pipeline step migrates instead — on a scale-to-zero host this would otherwise run on every cold start, and parallel starts would race for the same lock. With it off, startup fails rather than serving against a schema it does not expect. |

## Auth — tokens

| Setting | Default | |
| --- | --- | --- |
| `AUTH_SECRET_KEY` | — (required) | Signs access tokens. Generate a fresh one per deployment: `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `AUTH_ALGORITHM` | `HS256` | |
| `AUTH_ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | |

## Auth — login throttling

Failed password logins are counted, per account and per calling address.
Every value here has a working default; the whole block can be deleted.
Applies to `/token` and the docs login, which share one code path — API keys
are not throttled, since they are 32 random bytes with no dictionary to
attack.

| Setting | Default | |
| --- | --- | --- |
| `AUTH_LOCKOUT_ENABLED` | `true` | Off disables both counters. Meant for the test suite, not a deployment — a lockout switched off locally is a lockout nobody notices is broken. |
| `AUTH_LOCKOUT_MAX_ATTEMPTS` | `5` | Failed attempts within the window that lock an account. |
| `AUTH_LOCKOUT_WINDOW_MINUTES` | `15` | How far back that window reaches. Fixed, not sliding — a patient attacker retains a sustained four-guesses-per-window at these values. |
| `AUTH_LOCKOUT_BASE_MINUTES` | `15` | How long the first lock lasts. Each further lock without a successful login in between doubles it: 15, then 30, then 60. |
| `AUTH_LOCKOUT_PERMANENT_AFTER_LOCKS` | `4` | Which lock stops being temporary. After this many, an admin has to run `POST /users/{id}/unlock` or `python -m unlock_user`. A successful login resets the ladder, so an account in daily use cannot be walked up to this by an outsider. A lock never blocks an API key, which is what keeps a locked-out admin able to unlock themselves. |
| `AUTH_IP_MAX_FAILURES` | `20` | Same idea, keyed on the caller's address — catches one guess each across a list of usernames, where no single account sees enough failures to lock. |
| `AUTH_IP_WINDOW_MINUTES` | `15` | |
| `AUTH_IP_BAN_MINUTES` | `15` | Bans here are always temporary; an address is a lease, not an identity. |
| `AUTH_TRUSTED_PROXY_HOPS` | `1` | How many proxies sit in front of this process, counted from the **right** of `X-Forwarded-For` — not the leftmost entry, since a client can send that header itself and a proxy appends rather than replaces it. `1` is right for a single reverse proxy in front with no load balancer behind it; add one per additional proxy. `0` turns address throttling off, which is the honest setting when nothing trustworthy sets the header — including a service exposed directly, where the header cannot be believed at all. Getting this wrong throttles every caller as the proxy. |

## Bootstrap admin (first start only)

Migrations seed no users. On a database with no admin, and only then, these
create one; afterwards they are ignored, so they can stay set in a compose
file without recreating anything on restart. Leave unset to create the admin
by hand instead: `python -m bootstrap_admin`.

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
| `OAUTH_ALLOWED_REDIRECT_HOSTS` | `claude.ai` | Hosts a Dynamic-Client-Registration request's `redirect_uri` may target. Comma-separated, any number of entries, one leading `*.` wildcard per entry. `claude.ai` covers Claude web, Desktop, mobile and Cowork; Claude Code's loopback redirect is allowed by a built-in rule and must not be listed here. |
| `OAUTH_REGISTRATION_ENABLED` | `false` | Whether `POST /oauth/register` accepts anonymous Dynamic Client Registration. Off by default: the endpoint cannot require a credential, so leaving it on is an unauthenticated database write anyone who finds it can repeat. Closed, it 404s and the metadata document omits `registration_endpoint` entirely. Register clients with `python -m register_oauth_client` instead — both Claude Code (`--client-id`) and claude.ai (Advanced settings) accept one. |

## Browser client (`clients/web/`)

| Setting | Default | |
| --- | --- | --- |
| `CLIENT_ALLOWED_ORIGINS` | empty | Origins allowed to call the authenticated routes (`/token`, `/documents`, `/api-keys`, `/users/*`) from a browser. Comma-separated, empty by default — no browser can read these responses until set. Serving `clients/web/index.html` over a local static server needs exactly one entry here. |
| `CLIENT_HTML_PATH` | unset | Serve the client same-origin instead: set this and `GET /client` returns that file. Same-origin means `CLIENT_ALLOWED_ORIGINS` does not even need to name it. Unset by default, so the API depends on no build artifact and nothing changes for a deployment that does not want this. In the deploy image the path is `/app/clients/web/index.html`, where `COPY . /app` puts it. |

**On adding `null` to `CLIENT_ALLOWED_ORIGINS`.** Add it only to open
`clients/web/index.html` straight off disk, and prefer a `http://localhost:PORT`
origin if you have the choice. `null` is not just "the `file://` origin" — it
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
