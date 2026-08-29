"""Cross-origin access to the authenticated routes, for the browser client."""

import pytest


@pytest.fixture
def allowed_origins(monkeypatch):
    """One allowlisted origin, including the `null` file:// case.

    Settings are memoised for the life of the process, but conftest's
    `fresh_settings` drops that cache around every test, so setting the
    variable here is enough to have it read on the next request.
    """
    monkeypatch.setenv("CLIENT_ALLOWED_ORIGINS", "null,https://client.example.com")


class TestAllowlistedOrigin:
    async def test_echoes_the_origin(self, client, allowed_origins):
        response = await client.get("/documents", headers={"Origin": "null"})
        assert response.headers["access-control-allow-origin"] == "null"

    async def test_sets_vary(self, client, allowed_origins):
        response = await client.get("/documents", headers={"Origin": "null"})
        assert response.headers["vary"] == "Origin"

    async def test_keeps_a_vary_the_response_already_had(self, allowed_origins):
        """`Vary` is appended, not assigned.

        Nothing in this service sets one today, so this drives the middleware
        directly against an app that does — standing in for the compression
        middleware or MCP release that eventually will. Assigning would drop
        `Vary: Accept-Encoding`, and a dropped one in front of a cache serves
        somebody the wrong encoding.
        """
        from middleware.client_cors import ClientCorsMiddleware

        async def upstream(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"vary", b"Accept-Encoding")],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            """Never called on this path — the middleware only forwards it."""
            return {"type": "http.request", "body": b"", "more_body": False}

        await ClientCorsMiddleware(upstream)(
            {
                "type": "http",
                "method": "GET",
                "path": "/documents",
                "headers": [(b"origin", b"null")],
            },
            receive,
            send,
        )

        start = next(m for m in sent if m["type"] == "http.response.start")
        assert [v for k, v in start["headers"] if k == b"vary"] == [
            b"Accept-Encoding",
            b"Origin",
        ]

    async def test_a_second_allowlisted_origin_is_echoed_too(
        self, client, allowed_origins
    ):
        response = await client.get(
            "/documents", headers={"Origin": "https://client.example.com"}
        )
        assert (
            response.headers["access-control-allow-origin"]
            == "https://client.example.com"
        )

    async def test_preflight_short_circuits_with_204(self, client, allowed_origins):
        response = await client.options(
            "/documents",
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.status_code == 204
        assert response.headers["access-control-allow-origin"] == "null"
        assert response.headers["access-control-allow-methods"] == (
            "GET, POST, PATCH, DELETE, OPTIONS"
        )
        assert response.headers["access-control-allow-headers"] == (
            "authorization, content-type"
        )
        assert response.headers["access-control-max-age"] == "600"

    async def test_preflight_does_not_reach_the_route(self, client, allowed_origins):
        """No route on /documents has an OPTIONS handler; reaching one would
        405 rather than answer."""
        response = await client.options(
            "/documents",
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": "DELETE",
            },
        )
        assert response.status_code == 204


class TestUnlistedOrigin:
    async def test_gets_no_cors_headers(self, client, allowed_origins):
        response = await client.get(
            "/documents", headers={"Origin": "https://evil.example.com"}
        )
        assert "access-control-allow-origin" not in response.headers
        assert "vary" not in response.headers


class TestNoOrigin:
    async def test_gets_no_cors_headers(self, client, allowed_origins):
        response = await client.get("/documents")
        assert "access-control-allow-origin" not in response.headers


class TestPublicPathsAreUntouched:
    async def test_public_still_gets_exactly_one_wildcard_header(
        self, client, allowed_origins
    ):
        """PublicCorsMiddleware, not this one, owns /public/*. If both stamped
        it, the header would appear twice, which browsers reject."""
        response = await client.get(
            "/public/nobody/resume/nothing", headers={"Origin": "null"}
        )
        assert response.headers.get_list("access-control-allow-origin") == ["*"]


class TestEmptyAllowlist:
    """The default: nothing configured, nothing changes."""

    async def test_no_headers_even_with_an_origin(self, client):
        response = await client.get("/documents", headers={"Origin": "null"})
        assert "access-control-allow-origin" not in response.headers

    async def test_preflight_is_not_answered(self, client):
        """With no allowlisted origin this middleware does nothing, so the
        preflight falls through to FastAPI, which has no OPTIONS handler here."""
        response = await client.options(
            "/documents",
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": "DELETE",
            },
        )
        assert response.status_code != 204
