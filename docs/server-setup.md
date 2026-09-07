# Server Setup

Running this on a VPS with your own domain, HTTPS and Let's Encrypt
certificates. `mcp.example.com` stands in for your hostname throughout.

You need a host with Docker and Docker Compose, and a DNS `A`/`AAAA` record
pointing your domain at it.

```mermaid
flowchart LR
    NET[Internet] -->|443| CADDY["Caddy<br/>TLS + Let's Encrypt"]
    CADDY -->|8080, compose network| API[resume-api]
    API --> DB[(SQLite or Postgres)]
    API --> CLIENT["/client"]
    API --> MCP["/resume/mcp"]
    API --> FEED["/public/*"]
```

## 1. Firewall

| Port | Protocol | For |
| --- | --- | --- |
| `80` | TCP | Let's Encrypt HTTP challenge, and the redirect to HTTPS |
| `443` | TCP | HTTPS |
| `443` | UDP | HTTP/3 — optional |
| `22` | TCP | Your SSH access |

Nothing else. The API listens on `8080` inside the compose network only, so
**do not** open it — the proxy that terminates TLS is the only way in.

```bash
sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443 && sudo ufw enable
```

## 2. Configure

```bash
git clone https://github.com/tristankerner/resume-cv-mcp-api && cd resume-cv-mcp-api && cp .env.example .env
```

At minimum, set these in `.env`:

| Setting | Value |
| --- | --- |
| `ENVIRONMENT` | `production` |
| `AUTH_SECRET_KEY` | `python3 -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `MFA_ENCRYPTION_KEYS` | `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `PUBLIC_BASE_URL` | `https://mcp.example.com` — exactly, no trailing slash |
| `DOMAIN` | `mcp.example.com` |
| `ACME_EMAIL` | Your email, for certificate expiry notices |
| `BOOTSTRAP_ADMIN_USERNAME` | Your admin username |
| `BOOTSTRAP_ADMIN_PASSWORD` | 8+ chars with upper, lower, digit and symbol |

`ENVIRONMENT=production` puts `/docs`, `/redoc` and `/openapi.json` behind a
login, and makes `MFA_ENCRYPTION_KEYS` mandatory — startup refuses without it,
since a plaintext TOTP seed is symmetric.

`PUBLIC_BASE_URL` is compared against the `aud` claim of every OAuth token, so a
wrong value there rejects them all with no useful error.

## 3. Start it

```bash
docker compose build && docker compose -f docker-compose.caddy.yaml up -d --build
```

Two commands because Compose can't chain one service's image into another's
build context. The first produces the base image; the second layers the browser
client on and adds Caddy.

Caddy obtains a certificate on first start — give it thirty seconds, then:

```bash
curl -s https://mcp.example.com/ && docker compose -f docker-compose.caddy.yaml logs -f
```

Then open `https://mcp.example.com/client` and log in.

The bootstrap admin is created on first start only and ignored afterwards, so
those two settings can stay in `.env`. To create one by hand instead, leave
them unset and run `docker compose exec resume-api python -m admin_cli
bootstrap-admin`.

## 4. Register OAuth clients

**Only if you want a connector that speaks OAuth** rather than a static API
key. Skip this if an API key is enough — see [MCP Clients](mcp-clients.md).

Anonymous Dynamic Client Registration stays **off**: the endpoint can't ask who
is calling, so open it's an unauthenticated database write anyone who finds it
can repeat. Register clients yourself instead.

First set the hosts an authorization code may be delivered to:

```
OAUTH_ALLOWED_REDIRECT_HOSTS=claude.ai
```

| | |
| --- | --- |
| Entries are **hosts**, not URLs | `claude.ai`, never `https://claude.ai/api/mcp/auth_callback` |
| `claude.ai` alone | Covers Claude web, Desktop, mobile and Cowork |
| Claude Code | Uses loopback, which is built in — don't list `localhost` |
| Wildcards | One leading `*.` per entry; `*.example.com` doesn't cover the apex |

A bad entry fails startup rather than being silently ignored, so a mistake
fails the deploy instead of the connection. Removing a host takes effect
immediately, including for clients registered while it was allowed.

Keep the list short — each entry is a host you're trusting not to have an open
redirect.

Then register a client. This needs `users:admin` **and** an interactive login,
so the token has to come from `POST /token`, not an API key:

```bash
TOKEN=$(curl -s -X POST https://mcp.example.com/token -d 'username=YOU&password=YOURPASS' | jq -r .access_token)
```

If the admin account has MFA enabled — and on a public deployment it should —
`/token` returns a challenge instead, and `$TOKEN` comes out `null`. Redeem it
first:

```bash
MFA=$(curl -s -X POST https://mcp.example.com/token -d 'username=YOU&password=YOURPASS' | jq -r .mfa_token)
```

```bash
TOKEN=$(curl -s -X POST https://mcp.example.com/token/mfa -d "mfa_token=$MFA&code=123456" | jq -r .access_token)
```

```bash
curl -sS -X POST https://mcp.example.com/oauth-clients -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"client_name":"Claude","redirect_uris":["https://claude.ai/api/mcp/auth_callback"]}'
```

The response carries a `client_secret`, **shown once**. Paste it and the
`client_id` into the connector's Advanced settings.

| Field | Effect |
| --- | --- |
| `"public": true` | No secret; PKCE alone binds the exchange. For a client that can't keep one. |
| `"scope"` | Space-delimited default scopes |

**Claude Code needs a pinned port.** Its redirect is loopback on an ephemeral
port, and redirect URIs are matched exactly — pick one, register it, and pass
the same one to `claude mcp add --callback-port`:

```bash
curl -sS -X POST https://mcp.example.com/oauth-clients -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"client_name":"Claude Code","redirect_uris":["http://127.0.0.1:41703/callback"],"public":true}'
```

`GET /oauth-clients` lists what's registered; `DELETE /oauth-clients/{id}`
deregisters one, invalidating every authorization code and refresh token it
holds in the same request. Access tokens already issued survive until they
expire (30 minutes by default) — to cut someone off sooner, deactivate the
user, which takes effect on the next call.

### Verify

```bash
curl -sS https://mcp.example.com/.well-known/oauth-authorization-server | jq
```

Expect `authorization_endpoint`, `token_endpoint`, `revocation_endpoint` and
`code_challenge_methods_supported: ["S256"]`. `registration_endpoint` should be
**absent** — its presence means anonymous registration is on when you didn't
intend it.

## 5. Rate limiting

`/oauth/token` and `/token` are unauthenticated by necessity and deserve a rate
limit at the proxy, keyed on IP. The application's own per-account and
per-address lockout is a backstop against a sustained attack, not a substitute —
it doesn't stop the requests arriving.

Caddy's rate limiting needs a plugin (`caddy-ratelimit`) built in with
`xcaddy`, so the shipped `Caddyfile` leaves it out. If your VPS sits behind a
CDN, that's the easier place for the rule.

Leave `/oauth/authorize` and `/resume/mcp` unlimited: the former is interactive,
and the latter is authenticated and legitimately bursty during a tailoring run.

## Configuration reference

Every setting the service reads. Configuration is read once per process —
changing a value needs a restart.

**"Default" means the code's fallback**, applied when the setting is absent
entirely. Settings marked required have none. `.env.example` ships a working
value for each of those, so a copied `.env` starts as-is.

### Environment

| Setting | Default | |
| --- | --- | --- |
| `ENVIRONMENT` | `development` | `development` or `production`. Development leaves `/docs`, `/redoc` and `/openapi.json` open; production puts all three behind a login. Nothing else branches on this. |

### Database

| Setting | Default | |
| --- | --- | --- |
| `DATABASE_URL` | required (`.env.example`: `sqlite+aiosqlite:///data/app.db`) | SQLite needs nothing else. For Postgres: `postgresql+asyncpg://USER:PASS@HOST/resume_api?ssl=require` — one setting drives both the app and the migrations. |
| `DATABASE_ECHO` | `false` | Logs every statement with bind parameters. Leave off — user writes carry password hashes. |
| `RUN_MIGRATIONS_ON_STARTUP` | `true` | Runs `alembic upgrade head` before the first request. Turn off where a separate pipeline step migrates; startup then fails rather than serving against an unexpected schema. |

### Auth — tokens

| Setting | Default | |
| --- | --- | --- |
| `AUTH_SECRET_KEY` | required | Signs access tokens, MFA challenges and the docs cookie. Generate fresh per deployment. |
| `AUTH_ALGORITHM` | required (`HS256`) | |
| `AUTH_ACCESS_TOKEN_EXPIRE_MINUTES` | required (`30`) | Access token lifetime. |
| `DOCS_SESSION_MINUTES` | `60` | How long a documentation login lasts. |

### Auth — login throttling

Every value has a working default; the whole block can be deleted. See
[Security](security.md#login-throttling).

| Setting | Default | |
| --- | --- | --- |
| `AUTH_LOCKOUT_ENABLED` | `true` | Off disables both counters. Meant for the test suite, not a deployment. |
| `AUTH_LOCKOUT_MAX_ATTEMPTS` | `5` | Failures within the window that lock an account. |
| `AUTH_LOCKOUT_WINDOW_MINUTES` | `15` | How far back that window reaches. Fixed, not sliding. |
| `AUTH_LOCKOUT_BASE_MINUTES` | `15` | First lock duration; each further lock doubles it. |
| `AUTH_LOCKOUT_PERMANENT_AFTER_LOCKS` | `4` | Which lock stops being temporary. A successful login resets the ladder. |
| `AUTH_IP_MAX_FAILURES` | `20` | Same idea keyed on the caller's address. |
| `AUTH_IP_WINDOW_MINUTES` | `15` | |
| `AUTH_IP_BAN_MINUTES` | `15` | Address bans are always temporary. |
| `AUTH_TRUSTED_PROXY_HOPS` | `1` | Proxies in front of this process, counted from the **right** of `X-Forwarded-For`. `1` is correct for the Caddy setup above. `0` turns address throttling off, which is the honest setting when nothing trustworthy sets the header. Getting this wrong throttles every caller as the proxy. |

### Auth — multi-factor

MFA is per-account and opt-in; there's no service-wide switch.

| Setting | Default | |
| --- | --- | --- |
| `MFA_CHALLENGE_TTL_MINUTES` | `5` | How long a challenge stays redeemable. Long enough to find a phone, short enough that one left in shell history is worthless. |
| `MFA_TOTP_DRIFT_STEPS` | `1` | Thirty-second steps either side of now. `1` tolerates ~90s of clock skew; `0` will generate support requests. |
| `MFA_BACKUP_CODE_COUNT` | `10` | Codes per set. Regenerating replaces the set outright. |
| `MFA_ISSUER` | `resume-cv-mcp-api` | Name shown beside the account in an authenticator app. Cosmetic. |
| `MFA_ENCRYPTION_KEYS` | required in production | Fernet keys sealing TOTP secrets at rest, comma-separated, newest first. Every key listed can decrypt; the first encrypts. |

Deliver `MFA_ENCRYPTION_KEYS` through `SECRETS_DIR` rather than a bare
environment variable — `docker inspect` reads env vars. Rotation is
prepend-and-wait. **Losing every configured key locks every enrolled account
out of password login**; startup catches a mismatch and refuses to start rather
than letting that surface as everyone's login breaking at once.

### Bootstrap admin (first start only)

Migrations seed no users. On a database with no admin, and only then, these
create one; afterwards they're ignored, so they're safe to leave set.

| Setting | Default | |
| --- | --- | --- |
| `BOOTSTRAP_ADMIN_USERNAME` | — | |
| `BOOTSTRAP_ADMIN_PASSWORD` | — | 8+ characters with upper, lower, digit and symbol. Startup fails if these are set but can't produce an admin. |
| `BOOTSTRAP_ADMIN_EMAIL` | — | |

### OAuth 2.1

| Setting | Default | |
| --- | --- | --- |
| `PUBLIC_BASE_URL` | `http://localhost:8000` outside production | The service's own issuer and resource URL. Set explicitly in production. |
| `OAUTH_ALLOWED_REDIRECT_HOSTS` | required (`claude.ai`) | Hosts a `redirect_uri` may target. Comma-separated; one leading `*.` wildcard per entry. An empty value is rejected at startup. |
| `OAUTH_REGISTRATION_ENABLED` | `false` | Whether `POST /oauth/register` accepts anonymous registration. Leave off; register clients with `POST /oauth-clients` instead. |

### Browser client

| Setting | Default | |
| --- | --- | --- |
| `CLIENT_HTML_PATH` | unset | Serve a client same-origin: set this and `GET /client` returns that file. The compose files above set it for you. |
| `CLIENT_ALLOWED_ORIGINS` | empty | Origins allowed to call the authenticated routes from a browser. Only needed when the client is served from a **different** origin than the API. Comma-separated. |

Serving the client same-origin via `CLIENT_HTML_PATH` means
`CLIENT_ALLOWED_ORIGINS` doesn't need to name anything, and the client pre-fills
its API base URL from its own origin. That's the arrangement both compose files
use.

### Docker

| Setting | Default | |
| --- | --- | --- |
| `SECRETS_DIR` | unset | Point at a secrets mount and any setting above can be supplied as a file instead — one file per lowercased name, e.g. `/run/secrets/bootstrap_admin_password`. |

## Postgres instead of SQLite

SQLite in the `resume-data` volume is the default and is genuinely fine for one
person. To use Postgres, point `DATABASE_URL` at it and change nothing else:

```
DATABASE_URL=postgresql+asyncpg://user:pass@host/resume_api?ssl=require
```

`alembic/env.py` rewrites this to `postgresql+psycopg` for migrations,
translating `ssl` to libpq's `sslmode`, so one setting drives both engines. The
`documents` table carries a no-update trigger per dialect, so a revision can't
be rewritten either way.

## Backups

Two things to back up, **separately**:

| What | Where |
| --- | --- |
| The database | `resume-data` volume, or your Postgres host |
| `AUTH_SECRET_KEY` and `MFA_ENCRYPTION_KEYS` | Somewhere that is not beside the database backup |

A backup holding both the encrypted TOTP secrets and the key that opens them is
a backup with no encryption.

```bash
docker compose -f docker-compose.caddy.yaml exec resume-api sh -c 'cat /app/data/app.db' > backup-$(date +%F).db
```

## Updating

```bash
git pull && docker compose build && docker compose -f docker-compose.caddy.yaml up -d --build
```

Migrations run at startup by default, so a schema change is applied as the new
container comes up. `CLIENT_REF=<sha-or-tag>` pins which commit of the browser
client gets built; unset, it tracks `main`.
