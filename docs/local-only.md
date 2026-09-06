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
```

Leave `DATABASE_URL` unset — the default is SQLite at `data/app.db`, which is
the whole local story: no service to run, no connection string to get right.

```bash
uv run fastapi dev
```

`BOOTSTRAP_ADMIN_USERNAME`/`PASSWORD` create the admin on first start only, and
are ignored on every start after — safe to leave in `.env`.

## Adding the browser client

Optional, and a separate repository — this API has no dependency on it. Clone
[`resume-cv-mcp-api-web-client`](https://github.com/tristankerner/resume-cv-mcp-api-web-client)
anywhere, build it, and point `CLIENT_HTML_PATH` at the build output. (Or skip
this by hand and use `docker-compose.web-client.yaml` instead — see the
README's "Setup".)

```bash
git clone https://github.com/tristankerner/resume-cv-mcp-api-web-client clients
cd clients && npm ci && npm run build
```

```bash
CLIENT_HTML_PATH=./clients/dist/index.html
```

The build produces one self-contained HTML file; `clients/index.html` is the
Vite entry stub and is not servable on its own — point this at `dist/`, or the
page loads blank while it asks this API for `/src/main.tsx`. Node is needed
for the build and for nothing else here.

`GET /client` then serves it same-origin, so `CLIENT_ALLOWED_ORIGINS` does not
need to name anything: open `http://localhost:8000/client` and log in with the
bootstrap admin credentials. If the path does not exist the route is not
registered and a warning names the file it looked for; nothing else changes.

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
