# Build stage: resolve the environment with uv, into a self-contained venv that
# the runtime stage copies whole. Nothing from this stage but /opt/venv ships,
# so uv itself, the wheel cache and the lockfile stay out of the final image.
#
# Alpine rather than Debian slim: it is about 55 MB smaller as a base, and every
# wheel this project needs — asyncpg, psycopg, cryptography, pydantic-core,
# uvloop, argon2 — now publishes musllinux builds, so nothing compiles from
# source. If a future dependency has no musl wheel the build will fail loudly at
# this step rather than degrade; the fix is to move both stages back to
# python:3.14-slim-bookworm, which costs image size and nothing else.
FROM python:3.14-alpine AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Only the export, and only in its own layer: dependencies change far less often
# than source, so an edit to a router reuses this install. requirements.txt is a
# fully-pinned `uv export` carrying hashes, which uv verifies on install — the
# build cannot silently drift from uv.lock.
COPY requirements.txt /app/requirements.txt
RUN uv venv /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-cache -r /app/requirements.txt

# Wheels ship their shared objects unstripped, and the debug symbols are dead
# weight in a container — around 25 MB of it across cryptography, pydantic-core,
# asyncpg and uvloop. binutils is installed only here; this whole stage is
# discarded. Bundled test suites go the same way, for the same reason.
RUN apk add --no-cache binutils \
 && find /opt/venv -name '*.so' -exec strip --strip-unneeded {} + \
 && find /opt/venv -type d \( -name tests -o -name test \) -prune -exec rm -rf {} +


# Runtime stage.
FROM python:3.14-alpine AS runtime

# Unbuffered so Cloud Run's log tail is not a revision behind; no .pyc writes,
# since the venv is already compiled and the filesystem is ephemeral.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8080

# Nothing in the image needs to be written to at runtime, and on Cloud Run the
# only writable path is /tmp anyway. Running as a non-root user with a
# non-writable /app makes that explicit rather than incidental.
RUN adduser -D -u 10001 app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=root:root . /app

# The one writable path. Only the SQLite default needs it — a Postgres
# DATABASE_URL never touches it — but leaving it unwritable turns a local
# container run into a confusing permissions error instead of a working default.
RUN mkdir -p /app/data && chown app:app /app/data

USER app

EXPOSE 8080

# uvicorn directly rather than `fastapi run`: the CLI that provides the latter
# lives in fastapi[standard], which is a dev dependency now. This is what
# `fastapi run` does anyway.
#
# --proxy-headers with a trusted-any allow list, because the only route to this
# container is Cloud Run's front end, which terminates TLS and sets
# X-Forwarded-Proto. Without it the app believes every request arrived over
# plain HTTP and generates http:// URLs in redirects and in the OpenAPI schema.
#
# Shell form so $PORT — which Cloud Run injects, and which is not always 8080 —
# expands; exec so the server is PID 1 and receives SIGTERM directly, giving
# in-flight requests the shutdown lifespan rather than a hard kill.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
