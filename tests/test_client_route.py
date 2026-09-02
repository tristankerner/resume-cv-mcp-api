"""Serving the browser client same-origin, at GET /client."""

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from main import ClientRoute


def _app_serving(path: str | None) -> tuple[FastAPI, bool]:
    """A bare app with the route registered, rather than a reload of `main`.

    Registration happens once at import against the process's settings, so the
    shared `client` fixture can never exercise both branches. Driving the
    function directly is what makes the missing-file case testable at all.
    """
    app = FastAPI()
    registered = ClientRoute.register(app, path)
    return app, registered


class TestConfigured:
    def test_serves_the_file(self, tmp_path):
        page = tmp_path / "index.html"
        page.write_text("<title>resume-api client</title>")

        app, registered = _app_serving(str(page))
        assert registered

        response = TestClient(app).get("/client")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "resume-api client" in response.text

    def test_sends_the_hardening_headers(self, tmp_path):
        """This page is same-origin with every authenticated route once it is
        served from the API, and it carries a login form."""
        page = tmp_path / "index.html"
        page.write_text("<title>x</title>")

        response = TestClient(_app_serving(str(page))[0]).get("/client")
        for header, value in ClientRoute.HEADERS.items():
            assert response.headers[header] == value
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]

    def test_takes_no_credential(self, tmp_path):
        """It is a login page. Requiring a credential to reach it would be a
        chicken-and-egg problem, not a hardening measure."""
        page = tmp_path / "index.html"
        page.write_text("<title>x</title>")

        assert TestClient(_app_serving(str(page))[0]).get("/client").status_code == 200

    def test_stays_out_of_the_openapi_schema(self, tmp_path):
        """A page, not part of the API surface — the same reasoning
        routers/docs.py applies to itself."""
        page = tmp_path / "index.html"
        page.write_text("<title>x</title>")

        app, _ = _app_serving(str(page))
        assert "/client" not in app.openapi()["paths"]


class TestNotConfigured:
    def test_unset_registers_nothing(self):
        app, registered = _app_serving(None)
        assert not registered
        assert TestClient(app).get("/client").status_code == 404

    def test_a_missing_file_registers_nothing_and_warns(self, tmp_path, caplog):
        """A 404 that never changes would look like a routing bug. The warning
        names the setting so the cause is in the startup log instead."""
        missing = tmp_path / "not-there.html"

        with caplog.at_level(logging.WARNING, logger="uvicorn"):
            app, registered = _app_serving(str(missing))

        assert not registered
        assert TestClient(app).get("/client").status_code == 404
        assert "CLIENT_HTML_PATH" in caplog.text

    def test_a_directory_is_not_a_file(self, tmp_path):
        """`is_file()`, not `exists()` — pointing this at client/ rather than
        client/index.html is the likely typo, and FileResponse on a directory
        would fail per request instead of once at startup."""
        _, registered = _app_serving(str(tmp_path))
        assert not registered
