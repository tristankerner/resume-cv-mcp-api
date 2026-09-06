# Running it

Three ways to run the service, end to end. All three read the same settings —
see [configuration.md](configuration.md).

For the fully offline path — no domain, no TLS, SQLite, a bootstrap admin —
see [local-only.md](local-only.md) instead. This page assumes you already
have somewhere to point `DATABASE_URL` and just want the process running.

## Native Python

```bash
uv sync
cp .env.example .env   # then fill in AUTH_SECRET_KEY at minimum
uv run fastapi dev
```

`ENVIRONMENT` defaults to `development`, which leaves `/docs`, `/redoc` and
`/openapi.json` open. Migrations run at startup unless
`RUN_MIGRATIONS_ON_STARTUP=false`.

The first start of an empty database has no users. Either set
`BOOTSTRAP_ADMIN_USERNAME` and `BOOTSTRAP_ADMIN_PASSWORD` in `.env` — ignored
once an admin exists — or create one by hand:

```bash
uv run python -m admin_cli bootstrap-admin
```

## Docker

```bash
docker build -t resume-cv-mcp-api .
docker run --rm -p 8000:8080 \
  -e PORT=8080 \
  -e AUTH_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(64))") \
  -e BOOTSTRAP_ADMIN_USERNAME=admin \
  -e BOOTSTRAP_ADMIN_PASSWORD='Ch4nge-Me!' \
  -v resume-data:/app/data \
  resume-cv-mcp-api
```

The image sets `PORT=8080` and expects whatever's in front of it to map a
host port to that — Cloud Run's contract, reused here so the container is
exercised the same way locally as deployed. The volume matters: the image runs
as a non-root user (uid 10001), and a bind-mounted host directory keeps its
host ownership, so SQLite could not create its journal file there. A named
volume inherits the mountpoint's ownership from the image instead. If you want
to open the SQLite file directly from the host, run the app outside the
container — the native-Python path above.

To bake the browser client into the image, copy one into the build context
before building and set `CLIENT_HTML_PATH` to where `COPY . /app` puts it — see
the README's "Browser client". The image builds and runs fine without one;
`GET /client` just is not registered.

## Docker Compose

```bash
cp .env.example .env   # then fill in AUTH_SECRET_KEY at minimum
docker compose up --build
```

`docker-compose.yaml` wires up the same named volume (`resume-data`) and the
same `PORT=8080` contract as the bare Docker path above, plus `env_file:
./.env` so you don't have to repeat every variable on the command line. The
service is reachable at `localhost:8000`.
