"""The documentation endpoints, and the login in front of them in production."""

import jwt
import pytest

from persistence.base import utcnow
from persistence.user import User
from services.auth.docs_session import DocsSessionToken
from services.config.config_service import ConfigServiceModel, Environment
from services.database.database_service import DatabaseService

DOC_PATHS = ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"]


async def docs_login(client, username: str, password: str, next_path: str = "/docs"):
    return await client.post(
        "/docs/login",
        data={"username": username, "password": password, "next": next_path},
        follow_redirects=False,
    )


def cookie_headers(response) -> dict[str, str]:
    """The `docs_session` cookie a login response set, reattached explicitly.

    httpx's cookie jar honours `Secure` and will not resend the cookie on
    this test client's plain-http base_url — a limitation of the in-process
    ASGI transport, not of the cookie: a real browser talking HTTPS in
    production sends it automatically. Reading it back out of the response
    and attaching it by hand is what lets these tests exercise a session
    across more than one request.
    """
    return {"Cookie": f"docs_session={response.cookies['docs_session']}"}


@pytest.fixture
def production(monkeypatch):
    """Run the next request as the deployed service.

    Settings are memoised for the life of the process, but conftest's
    `fresh_settings` drops that cache around every test, so setting the
    variable here is enough to have it read on the next request.
    """
    monkeypatch.setenv("ENVIRONMENT", "production")


class TestEnvironmentSetting:
    def test_defaults_to_development(self, monkeypatch):
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        settings = ConfigServiceModel()
        assert settings.environment is Environment.DEVELOPMENT
        assert settings.is_production is False

    def test_production_is_recognised(self, production):
        assert ConfigServiceModel().is_production is True

    def test_case_and_whitespace_are_forgiven(self, monkeypatch):
        """Deployment platforms are not consistent about case, and being strict
        about it means a service that will not start."""
        monkeypatch.setenv("ENVIRONMENT", " Production ")
        assert ConfigServiceModel().is_production is True

    def test_an_unrecognised_value_is_rejected(self, monkeypatch):
        """Not silently development: a typo would then be an open deployment."""
        monkeypatch.setenv("ENVIRONMENT", "prod")
        with pytest.raises(ValueError):
            ConfigServiceModel()


class TestDevelopment:
    @pytest.mark.parametrize("path", DOC_PATHS)
    async def test_needs_no_credential(self, client, path):
        assert (await client.get(path)).status_code == 200

    async def test_swagger_ui_points_at_the_schema(self, client):
        assert "/openapi.json" in (await client.get("/docs")).text

    async def test_schema_is_the_real_one(self, client):
        """The route is ours now rather than FastAPI's, so it has to be checked
        that it still serves the application's own schema."""
        schema = (await client.get("/openapi.json")).json()
        assert "/token" in schema["paths"]

    @pytest.mark.parametrize("path", DOC_PATHS)
    async def test_the_docs_are_not_in_the_docs(self, client, path):
        assert path not in (await client.get("/openapi.json")).json()["paths"]


class TestProduction:
    @pytest.mark.parametrize("path", DOC_PATHS)
    async def test_refuses_an_anonymous_request(self, client, production, path):
        response = await client.get(path, follow_redirects=False)
        assert response.status_code == 303

    async def test_the_redirect_goes_to_the_login_page(self, client, production):
        """Without this a browser shows a bare error rather than somewhere to
        sign in, and there is no way to supply credentials at all."""
        response = await client.get("/docs", follow_redirects=False)
        assert response.headers["location"] == "/docs/login?next=/docs"

    @pytest.mark.parametrize("path", DOC_PATHS)
    async def test_serves_a_user_with_a_valid_session_cookie(
        self, client, production, admin, path
    ):
        login = await docs_login(client, admin.username, admin.password)
        assert login.status_code == 303
        assert "Secure" in login.headers["set-cookie"]

        response = await client.get(path, headers=cookie_headers(login))
        assert response.status_code == 200

    async def test_any_account_will_do(self, client, production, roleless):
        """This gates who sees the route list, not what they may call: every
        route behind it enforces its own scopes."""
        login = await docs_login(client, roleless.username, roleless.password)
        response = await client.get("/openapi.json", headers=cookie_headers(login))
        assert response.status_code == 200

    async def test_rejects_the_wrong_password(self, client, production, admin):
        response = await docs_login(client, admin.username, "not-the-password")
        assert response.status_code == 401
        assert "Incorrect username or password" in response.text

    async def test_rejects_an_unknown_user(self, client, production, password):
        response = await docs_login(client, "nobody-at-all", password)
        assert response.status_code == 401

    async def test_a_wrong_password_does_not_set_a_cookie(
        self, client, production, admin
    ):
        await docs_login(client, admin.username, "not-the-password")
        response = await client.get("/docs", follow_redirects=False)
        assert response.status_code == 303

    async def test_rejects_a_bearer_token(self, client, production, admin):
        """A JWT is not a docs session cookie. The docs are opened by a
        browser, which has nowhere to keep a token, so only the cookie path
        works here."""
        response = await client.get(
            "/docs", headers=admin.headers, follow_redirects=False
        )
        assert response.status_code == 303

    async def test_rejects_a_forged_cookie(self, client, production):
        client.cookies.set("docs_session", "not-a-valid-token")
        response = await client.get("/docs", follow_redirects=False)
        assert response.status_code == 303

    async def test_rejects_a_cookie_with_a_non_string_subject(
        self, client, production, admin
    ):
        settings = ConfigServiceModel()
        forged = jwt.encode(
            {"sub": 1, "token_use": DocsSessionToken.TOKEN_USE},
            settings.auth_secret_key.get_secret_value(),
            algorithm=settings.auth_algorithm,
        )
        client.cookies.set("docs_session", forged)
        response = await client.get("/docs", follow_redirects=False)
        assert response.status_code == 303

    async def test_rejects_a_cookie_with_a_non_numeric_subject(
        self, client, production
    ):
        settings = ConfigServiceModel()
        forged = jwt.encode(
            {"sub": "not-a-number", "token_use": DocsSessionToken.TOKEN_USE},
            settings.auth_secret_key.get_secret_value(),
            algorithm=settings.auth_algorithm,
        )
        client.cookies.set("docs_session", forged)
        response = await client.get("/docs", follow_redirects=False)
        assert response.status_code == 303

    async def test_visiting_login_with_a_valid_session_redirects_straight_through(
        self, client, production, admin
    ):
        login = await docs_login(client, admin.username, admin.password)
        response = await client.get(
            "/docs/login?next=/redoc",
            headers=cookie_headers(login),
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/redoc"

    async def test_rejects_a_deactivated_user(
        self, client, production, password, hashed_password
    ):
        """The same rules as /token, because it is the same call: deactivating
        a user closes this door with the rest of them."""
        async with DatabaseService.session() as db:
            db.add(
                User(
                    username="deactivated",
                    password=hashed_password,
                    roles=[],
                    active=False,
                )
            )
            await db.commit()

        response = await docs_login(client, "deactivated", password)
        assert response.status_code == 401

    async def test_a_session_stops_working_once_the_account_is_deactivated(
        self, client, production, admin
    ):
        login = await docs_login(client, admin.username, admin.password)
        headers = cookie_headers(login)
        assert (await client.get("/docs", headers=headers)).status_code == 200

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, admin.user_id)
            assert user is not None
            user.active = False
            await db.commit()

        response = await client.get("/docs", headers=headers, follow_redirects=False)
        assert response.status_code == 303

    async def test_a_session_stops_working_once_the_password_changes(
        self, client, production, admin
    ):
        login = await docs_login(client, admin.username, admin.password)
        headers = cookie_headers(login)
        assert (await client.get("/docs", headers=headers)).status_code == 200

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, admin.user_id)
            assert user is not None
            user.password = "a-different-hash"
            await db.commit()

        response = await client.get("/docs", headers=headers, follow_redirects=False)
        assert response.status_code == 303

    async def test_rejects_a_passwordless_account(self, client, production, password):
        """Service accounts are provisioned without one and cannot log in."""
        async with DatabaseService.session() as db:
            db.add(User(username="passwordless-docs", password=None, roles=[]))
            await db.commit()

        response = await docs_login(client, "passwordless-docs", password)
        assert response.status_code == 401

    async def test_next_is_restricted_to_the_known_docs_paths(
        self, client, production, admin
    ):
        """An open-redirect guard: `next` is attacker-controlled input, and
        the only acceptable destinations are the docs paths this router
        itself serves."""
        login = await docs_login(
            client, admin.username, admin.password, next_path="https://evil.example/"
        )
        assert login.status_code == 303
        assert login.headers["location"] == "/docs"

    async def test_logout_clears_the_session(self, client, production, admin):
        login = await docs_login(client, admin.username, admin.password)
        headers = cookie_headers(login)
        assert (await client.get("/docs", headers=headers)).status_code == 200

        logout = await client.post(
            "/docs/logout", headers=headers, follow_redirects=False
        )
        assert logout.status_code == 303
        # A browser deletes the cookie once it sees Max-Age=0; a stateless
        # design has no server-side session to revoke, so this is the whole
        # of what logout does.
        assert "Max-Age=0" in logout.headers["set-cookie"]


class TestProductionMfaLogin:
    """The docs login's second step, for an account with a second factor."""

    async def test_an_enrolled_account_is_shown_a_code_form(
        self, client, production, enrolled
    ):
        response = await docs_login(
            client, enrolled.actor.username, enrolled.actor.password
        )
        assert response.status_code == 200
        assert "<form" in response.text
        assert 'name="mfa_token"' in response.text
        assert 'name="code"' in response.text

    async def test_a_valid_code_completes_the_login(
        self, client, production, enrolled, totp_code
    ):
        challenge = await docs_login(
            client, enrolled.actor.username, enrolled.actor.password
        )
        mfa_token = self._extract_mfa_token(challenge.text)

        response = await client.post(
            "/docs/login",
            data={
                "mfa_token": mfa_token,
                "code": totp_code(enrolled.secret),
                "next": "/docs",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        docs = await client.get("/docs", headers=cookie_headers(response))
        assert docs.status_code == 200

    async def test_a_wrong_code_re_shows_the_form(self, client, production, enrolled):
        challenge = await docs_login(
            client, enrolled.actor.username, enrolled.actor.password
        )
        mfa_token = self._extract_mfa_token(challenge.text)

        response = await client.post(
            "/docs/login",
            data={"mfa_token": mfa_token, "code": "000000", "next": "/docs"},
        )
        assert response.status_code == 401
        assert "not valid" in response.text

    async def test_an_expired_or_forged_token_falls_back_to_the_login_form(
        self, client, production, enrolled
    ):
        response = await client.post(
            "/docs/login",
            data={"mfa_token": "not-a-real-token", "code": "000000", "next": "/docs"},
        )
        assert response.status_code == 401
        assert 'name="password"' in response.text

    async def test_a_docs_challenge_is_refused_at_slash_token_slash_mfa(
        self, client, production, enrolled
    ):
        """The three surfaces grant different things — a challenge from one
        must not be redeemable at another."""
        challenge = await docs_login(
            client, enrolled.actor.username, enrolled.actor.password
        )
        mfa_token = self._extract_mfa_token(challenge.text)

        response = await client.post(
            "/token/mfa", data={"mfa_token": mfa_token, "code": "000000"}
        )
        assert response.status_code == 401

    @staticmethod
    def _extract_mfa_token(html: str) -> str:
        marker = 'name="mfa_token" value="'
        start = html.index(marker) + len(marker)
        end = html.index('"', start)
        return html[start:end]


class TestProductionMfaLockRecheck:
    """A lock tripped between the password and the code has to be honoured
    here too — /token/mfa and /oauth/authorize both re-check it, and a
    surface that forgets is a surface the throttle can be walked around."""

    @staticmethod
    def _extract_mfa_token(html: str) -> str:
        marker = 'name="mfa_token" value="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    async def test_a_lock_tripped_between_the_steps_refuses_the_code(
        self, client, production, enrolled, totp_code
    ):
        challenge = await docs_login(
            client, enrolled.actor.username, enrolled.actor.password
        )
        assert challenge.status_code == 200
        mfa_token = self._extract_mfa_token(challenge.text)

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, enrolled.actor.user_id)
            assert user is not None
            user.locked_permanently_at = utcnow()
            await db.commit()

        response = await client.post(
            "/docs/login",
            data={
                "mfa_token": mfa_token,
                "code": totp_code(enrolled.secret),
                "next": "/docs",
            },
            follow_redirects=False,
        )
        assert response.status_code == 401
        assert "docs_session" not in response.headers.get("set-cookie", "")
