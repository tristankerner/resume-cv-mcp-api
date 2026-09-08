# Development

Running the service natively, without Docker.

## Setup

```bash
uv sync --all-groups && cp .env.example .env
```

Fill in `.env`:

```bash
AUTH_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(64))")
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=Ch4nge-Me!1
```

Leave `DATABASE_URL` at its default — SQLite at `data/app.db`, no service to
run.

```bash
uv run fastapi dev
```

`ENVIRONMENT` defaults to `development`, which leaves `/docs`, `/redoc` and
`/openapi.json` open. Migrations run at startup unless
`RUN_MIGRATIONS_ON_STARTUP=false`.

The bootstrap admin is created on first start only and ignored afterwards, so
it's safe to leave in `.env`. To create one by hand instead:

```bash
uv run python -m admin_cli bootstrap-admin
```

## Adding the browser client

The client is a [separate repository](https://github.com/tristankerner/resume-cv-mcp-api-web-client)
and optional — this API has no build-time dependency on one. To develop against
a checkout rather than a pinned commit:

```bash
git clone https://github.com/tristankerner/resume-cv-mcp-api-web-client clients && cd clients && npm ci && npm run build
```

```bash
CLIENT_HTML_PATH=./clients/dist/index.html
```

Point it at the **build output**, `dist/index.html` — `clients/index.html` is
the Vite entry stub, and serving it yields a blank page whose `/src/main.tsx`
request 404s against this API.

`GET /client` then serves it same-origin, so `CLIENT_ALLOWED_ORIGINS` doesn't
need to name anything. If the path doesn't exist the route isn't registered and
a warning names the file it looked for; nothing else changes.

`./clients` is the conventional name — `.dockerignore` expects it when keeping a
local build out of an image — but nothing enforces it.

To skip the manual checkout entirely, use `docker-compose.web-client.yaml`,
which builds the client from source and bakes it in.

## Running with Docker directly

```bash
docker build -t resume-cv-mcp-api .
```

```bash
docker run --rm -p 8000:8080 -e PORT=8080 -e AUTH_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(64))") -e BOOTSTRAP_ADMIN_USERNAME=admin -e BOOTSTRAP_ADMIN_PASSWORD='Ch4nge-Me!' -v resume-data:/app/data resume-cv-mcp-api
```

The image sets `PORT=8080` and expects whatever's in front of it to map a host
port to that.

**The named volume is deliberate, not a shortcut.** The image runs as a
non-root user (uid 10001), and a bind-mounted host directory keeps its host
ownership, so SQLite couldn't create its journal file there. A named volume
inherits the mountpoint's ownership from the image. To open the SQLite file
directly from the host, run natively instead.

## Tests

```bash
uv run pytest
```

```bash
uv run pytest --cov --cov-report=term-missing
```

The suite runs against a temporary database with a generated signing key, and
pins every setting it depends on before importing the application. It does not
read your `.env`, touch `data/app.db`, or assume any local configuration —
identifiers are read back from the rows the fixtures create rather than written
down. Migrations are the real ones, run once per session, and always on SQLite
regardless of what `DATABASE_URL` says.

CI runs the same suite with `--cov-fail-under=90` on every push
(`.github/workflows/test.yml`).

## Before committing

Formatting, linting and type checking run as a git hook rather than in CI.
Install it once per clone:

```bash
uv run pre-commit install
```

| Tool | Does |
| --- | --- |
| `ruff check --fix` | Linting, including import sorting |
| `ruff format` | Formatting |
| `ty check` | Type checking, over the whole project |

The first two rewrite files in place, so a commit they touch fails once and
needs the files re-staged. `ty` runs over everything rather than just staged
files, because a signature change surfaces in a different file than the one you
edited — and pre-commit stashes unstaged work first, so what gets checked is
what would actually land.

`alembic/versions/` is excluded from both tools; the scaffolding there is
generated and the schema is covered by tests instead. Note that pre-commit hands
ruff explicit filenames, which overrides the exclusion, so both ruff hooks pass
`--force-exclude`. Anything invoking ruff outside pre-commit needs the same flag
to see the same result.

To run them by hand:

```bash
uv run pre-commit run --all-files
```

## Application tracking

### Importing a job-hunt spreadsheet

`scripts/import_job_hunt.py` is a one-time import of a personal CSV export
into companies, applications and events. Dry run by default:

```bash
PYTHONPATH=. uv run python -m scripts.import_job_hunt \
    --username YOU --csv "data/2026 Job Hunt - Sheet1.csv"
```

Add `--write` to actually write, once the dry-run output — company names
flagged for review, unparseable dates, unmapped response values — looks
right. It runs as one transaction and is idempotent: re-running against the
same file finds every row's application already there and writes nothing new.

### SQLite audit trigger columns are literal

The audit-log migration generates three SQLite triggers (insert/update/delete)
per audited table, and each one names its audited columns explicitly in the
generated SQL — there is no SQLite equivalent of Postgres' "every column
except these". A future migration that adds, drops or renames a column on an
audited table (see [Security](security.md#audit-log) for the list) **must
drop and recreate that table's three triggers** in the same migration, or
they silently stop matching the live schema and start throwing on write.
`tests/test_audit_log.py`'s structural test is the alarm for this — it
compares `services/tracking/audit_columns.py`'s allowlist against the real
model columns and fails the day the two disagree.

## Layout

```
routers/            HTTP routes, and the MCP tools (mcp.py, mcp_tracking.py)
services/auth/      authentication, scopes, Principal, API keys, throttling, MFA
services/user/      user CRUD, first-admin bootstrap, new-account document seeding
services/oauth/     OAuth 2.1 authorization server and admin client registration
services/document/  document reads, writes, and the public projection
services/tracking/  companies, contacts, applications, events, attachments, audit
persistence/        SQLAlchemy models
middleware/         CORS for the public feed and for the browser client
alembic/            migrations, run automatically at startup
admin_cli.py        last-resort account recovery, direct against the database
examples/           fictional document fixtures, the seeding script, and SKILL.md
scripts/            one-off maintenance scripts (resume v2 migration, CSV import)
```

### Code conventions

`CLAUDE.md` is the authority. In short: async-first, strict type hints, and
everything lives in a class — no free-standing functions or module-level
variables, with the framework-forced exceptions documented there
(`alembic/versions/*` and `alembic/env.py`, `tests/`, the `@tool` adapters in
`routers/mcp.py` and `routers/mcp_tracking.py`, and `main.py`'s ASGI
entrypoint).

## Local MCP connection

Same as any other deployment, with `http://localhost:8000/resume/mcp` as the
URL and no TLS to worry about since nothing leaves the machine:

```bash
TOKEN=$(curl -s -X POST localhost:8000/token -d 'username=admin&password=Ch4nge-Me!1' | jq -r .access_token)
```

If you've enrolled a second factor on this account, `/token` returns a
challenge rather than a token — redeem it at `/token/mfa` first, as
[MCP Clients](mcp-clients.md#minting-an-api-key) shows.

```bash
curl -s -X POST localhost:8000/api-keys -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"name":"local-mcp","scopes":["resume:read","metadata:read","skill:read"]}' | jq -r .key
```

OAuth needs a stable public URL to serve as its issuer, so it has no local-only
story — a static API key is the credential to use here. See
[MCP Clients](mcp-clients.md).
