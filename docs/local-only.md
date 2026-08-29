# Running entirely local

The whole service, an admin account, the browser client and an MCP connection,
with no domain, no TLS, no cloud account, and no OAuth. Everything talks to
`localhost`.

```bash
uv sync
cp .env.example .env
```

Fill in `.env`:

```bash
AUTH_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(64))")
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=Ch4nge-Me!1
CLIENT_HTML_PATH=./clients/web/index.html
```

Leave `DATABASE_URL` unset — the default is SQLite at `data/app.db`, which is
the whole local story: no service to run, no connection string to get right.

```bash
uv run fastapi dev
```

`BOOTSTRAP_ADMIN_USERNAME`/`PASSWORD` create the admin on first start only, and
are ignored on every start after — safe to leave in `.env`. `CLIENT_HTML_PATH`
makes `GET /client` serve the browser client same-origin, so
`CLIENT_ALLOWED_ORIGINS` does not need to name anything: open
`http://localhost:8000/client` and log in with the bootstrap admin credentials.
That needs the `clients/` submodule checked out — `git submodule update
--init` if you cloned without `--recurse-submodules`.

## Pointing an MCP client at it

Mint a key scoped to the three reads, same as any other deployment — see the
README's "Pointing an MCP client at a key" for the full walkthrough and
[mcp-clients.md](mcp-clients.md) for wiring it into a specific client. The only
thing local-only about this step is the URL: `http://localhost:8000/resume/mcp`
instead of a public one, and no TLS to worry about since nothing leaves the
machine.

```bash
TOKEN=$(curl -s -X POST localhost:8000/token -d 'username=admin&password=Ch4nge-Me!1' | jq -r .access_token)
curl -s -X POST localhost:8000/api-keys -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"name":"local-mcp","scopes":["resume:read","metadata:read","skill:read"]}' | jq -r .key
```

## What this route skips

No `PUBLIC_BASE_URL` to get right, no `OAUTH_ALLOWED_REDIRECT_HOSTS`, no
reverse proxy, no `AUTH_TRUSTED_PROXY_HOPS` to reason about — those all matter
once something other than you is reaching this over a network. OAuth
specifically needs a stable public URL to serve as the issuer, so it has no
local-only story at all; a static API key is the only credential type worth
using here.
