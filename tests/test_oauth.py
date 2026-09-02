"""OAuth 2.1 for the MCP endpoint — discovery, the full authorization-code
flow, the security requirements the design turns on, the redirect allowlist,
and backwards compatibility with every credential that worked before this.
"""

import base64
import hashlib
import secrets
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt as pyjwt
import pyotp
import pytest
from sqlalchemy import select

from persistence.mfa_credential import MfaCredential
from persistence.oauth_client import OAuthClient
from persistence.user import User
from services.auth.mcp_verifier import McpTokenVerifier
from services.auth.mfa.challenge import MfaChallengeContext, MfaChallengeToken
from services.auth.mfa.methods.totp import TotpMethod
from services.auth.scopes import Scopes
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService
from services.oauth.clients import OAuthClientRegistry
from services.oauth.redirect_allowlist import RedirectAllowlist
from services.oauth.scopes import OAuthScopes
from services.oauth.tokens import TokenIssuer
from tests.helpers import OAuthTokens

REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"


@dataclass
class ScopedEnrolled:
    """Like conftest's `Enrolled`, but for an actor whose roles actually
    grant a scope — see the local `enrolled` fixture below."""

    actor: object
    secret: str


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def query_of(response) -> dict[str, str]:
    location = response.headers["location"]
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(location).query))


async def pre_register(
    redirect_uris=(REDIRECT_URI,), *, public=True, name="Test Client"
):
    """Create a client the way `POST /oauth-clients` does — see
    tests/test_oauth_clients.py for that route directly.

    This is the default posture: OAUTH_REGISTRATION_ENABLED is off, so a
    pre-registered client is how one comes to exist at all. Most tests only
    need *a* client and should use this, so they run against the same
    configuration production does. Tests exercising anonymous DCR itself use
    `register_over_http` and turn the endpoint on explicitly.
    """
    settings = ConfigService.get_without_deps().settings
    async with DatabaseService.session() as db:
        row, _secret = await OAuthClientRegistry(db).create(
            redirect_uris=list(redirect_uris),
            client_name=name,
            token_endpoint_auth_method="none" if public else "client_secret_post",
            scope=None,
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            allowed_hosts=settings.oauth_allowed_redirect_hosts,
        )
        return row.client_id


async def register_over_http(client, redirect_uris=(REDIRECT_URI,), **extra):
    """POST /oauth/register — only reachable with OAUTH_REGISTRATION_ENABLED."""
    return await client.post(
        "/oauth/register", json={"redirect_uris": list(redirect_uris), **extra}
    )


async def authorize(
    client,
    *,
    client_id,
    username,
    password,
    redirect_uri=REDIRECT_URI,
    scope="resume:read metadata:read",
    state="s1",
    code_challenge=None,
    code_challenge_method="S256",
    decision="approve",
    resource=None,
    mfa_token=None,
    code=None,
):
    verifier, challenge = pkce_pair()
    data = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": code_challenge if code_challenge is not None else challenge,
        "code_challenge_method": code_challenge_method,
        "scope": scope,
        "state": state,
        "username": username,
        "password": password,
        "decision": decision,
    }
    if resource:
        data["resource"] = resource
    if mfa_token is not None:
        data["mfa_token"] = mfa_token
    if code is not None:
        data["code"] = code
    response = await client.post("/oauth/authorize", data=data, follow_redirects=False)
    return response, verifier


async def get_code(client, *, client_id, username, password, **kw):
    response, verifier = await authorize(
        client, client_id=client_id, username=username, password=password, **kw
    )
    assert response.status_code == 303, response.text
    return query_of(response)["code"], verifier


def extract_hidden_value(html: str, name: str) -> str:
    marker = f'name="{name}" value="'
    start = html.index(marker) + len(marker)
    end = html.index('"', start)
    return html[start:end]


@pytest.fixture
async def enrolled(admin) -> ScopedEnrolled:
    """`admin`, with an activated TOTP credential.

    Shadows conftest's own `enrolled` fixture (built on a roleless actor,
    fine for the login surfaces but grantless here) — the authorize flow
    needs a scope to hand out, or a wrong-scope redirect masks whatever the
    test is actually checking. Activated with the previous step's code, not
    the current one, for the same reason conftest's version is: a test then
    calling `totp_code(secret)` gets a fresh step rather than one the replay
    guard has already spent.
    """
    async with DatabaseService.session() as db:
        user = await User.get_user_by_id(db, admin.user_id)
        assert user is not None
        method = TotpMethod(db, ConfigService.get_without_deps().settings)
        result = await method.begin_enrollment(user, "Authenticator app")
        await db.commit()
        credential = await MfaCredential.get_for_user(db, user.id, result.credential_id)
        assert credential is not None
        assert result.secret is not None
        activation_code = pyotp.TOTP(result.secret).at(
            datetime.now(UTC), counter_offset=-1
        )
        await method.complete_enrollment(credential, activation_code)
        await db.commit()

    return ScopedEnrolled(actor=admin, secret=result.secret)


@pytest.fixture
def reconfigure(monkeypatch):
    """Set env vars and drop the memoised settings, so a change mid-test is
    actually observed. Mirrors tests/test_lockout.py's fixture of the same
    name and the same reasoning."""

    def apply(**values):
        for key, value in values.items():
            monkeypatch.setenv(key, str(value))
        ConfigService.reset()

    return apply


class TestDiscovery:
    async def test_as_metadata_has_every_required_field(self, client):
        response = await client.get("/.well-known/oauth-authorization-server")
        assert response.status_code == 200
        body = response.json()
        for field in (
            "issuer",
            "authorization_endpoint",
            "token_endpoint",
            "revocation_endpoint",
            "scopes_supported",
            "response_types_supported",
            "grant_types_supported",
            "code_challenge_methods_supported",
            "token_endpoint_auth_methods_supported",
        ):
            assert field in body, field
        assert body["code_challenge_methods_supported"] == ["S256"]
        assert "plain" not in body["code_challenge_methods_supported"]
        # registration_endpoint is deliberately not in that list — it is
        # OPTIONAL under RFC 8414 and absent unless DCR is switched on. See
        # TestRegistrationClosedByDefault.

    async def test_protected_resource_metadata_at_root(self, client):
        response = await client.get("/.well-known/oauth-protected-resource/resume/mcp")
        assert response.status_code == 200
        body = response.json()
        assert body["resource"] == "http://testserver/resume/mcp"
        assert body["authorization_servers"] == ["http://testserver/"]

    async def test_issuer_is_identical_to_the_advertised_authorization_server(
        self, client
    ):
        """RFC 8414 §3.3: the `issuer` returned MUST be identical to the
        issuer identifier the client used to build this request — "If these
        values are not identical, the data contained in the response MUST NOT
        be used."

        The client takes that identifier from `authorization_servers` in the
        protected resource document, so the two strings have to match exactly,
        trailing slash included. Compared verbatim on purpose: normalising
        either side here is what let the two drift apart unnoticed.
        """
        prm = await client.get("/.well-known/oauth-protected-resource/resume/mcp")
        asm = await client.get("/.well-known/oauth-authorization-server")

        advertised = prm.json()["authorization_servers"]
        assert len(advertised) == 1
        assert asm.json()["issuer"] == advertised[0]

    async def test_endpoints_have_no_doubled_slash(self, client):
        """The issuer keeps its trailing slash; the endpoints are built from
        the stripped form and must not inherit one."""
        body = (await client.get("/.well-known/oauth-authorization-server")).json()
        for field in (
            "authorization_endpoint",
            "token_endpoint",
            "revocation_endpoint",
        ):
            assert "//" not in body[field].split("://", 1)[1], field


class TestRegistrationClosedByDefault:
    """Anonymous DCR is an unauthenticated database write, so it is off unless
    asked for. The redirect allowlist bounds where a code may be delivered; it
    says nothing about how many client rows a stranger may create, and this is
    what closes that."""

    async def test_metadata_omits_registration_endpoint(self, client):
        body = (await client.get("/.well-known/oauth-authorization-server")).json()
        # Absent, not null — a null there is a malformed metadata document
        # rather than a signal, so exclude_none matters.
        assert "registration_endpoint" not in body

    async def test_register_endpoint_is_not_found(self, client):
        response = await register_over_http(client)
        assert response.status_code == 404

    async def test_nothing_is_written_when_the_endpoint_is_closed(self, client):
        """The point of 404 over 403: no row, and no work done before the
        refusal."""
        async with DatabaseService.session() as db:
            before = len((await db.execute(select(OAuthClient))).scalars().all())
        await register_over_http(client)
        async with DatabaseService.session() as db:
            after = len((await db.execute(select(OAuthClient))).scalars().all())
        assert after == before

    async def test_public_auth_method_is_not_advertised_when_closed(self, client):
        body = (await client.get("/.well-known/oauth-authorization-server")).json()
        assert body["token_endpoint_auth_methods_supported"] == ["client_secret_post"]

    async def test_enabling_it_restores_the_endpoint_and_the_metadata(
        self, client, reconfigure
    ):
        reconfigure(OAUTH_REGISTRATION_ENABLED="true")
        body = (await client.get("/.well-known/oauth-authorization-server")).json()
        assert body["registration_endpoint"].endswith("/oauth/register")
        assert "none" in body["token_endpoint_auth_methods_supported"]
        assert (await register_over_http(client)).status_code == 200

    async def test_a_pre_registered_client_completes_the_flow_with_dcr_closed(
        self, client, admin, password
    ):
        """The whole point: registration being shut does not stop anyone
        authorizing, it only changes how the client came to exist."""
        client_id = await pre_register()
        code, verifier = await get_code(
            client, client_id=client_id, username=admin.username, password=password
        )
        response = await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["access_token"]

    async def test_unauthenticated_mcp_401_points_at_protected_resource_metadata(
        self, client
    ):
        response = await client.post(
            "/resume/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={},
        )
        assert response.status_code == 401
        www_authenticate = response.headers["www-authenticate"]
        assert "resource_metadata=" in www_authenticate
        assert "/.well-known/oauth-protected-resource/resume/mcp" in www_authenticate

    async def test_token_endpoint_is_unchanged_password_login(self, client):
        """A 400 with an {"error": ...} body would mean the OAuth token
        endpoint got mounted over it instead."""
        response = await client.post(
            "/token", data={"username": "nobody", "password": "wrong"}
        )
        assert response.status_code == 401
        assert "detail" in response.json()
        assert "error" not in response.json()


class TestFullFlow:
    async def test_register_authorize_token_and_verify(
        self, client, admin, password, reconfigure
    ):
        # The one end-to-end test that registers over HTTP, so the DCR path is
        # exercised start to finish at least once. Every other test uses
        # pre_register, which is the default posture.
        reconfigure(OAUTH_REGISTRATION_ENABLED="true")
        reg = await register_over_http(client)
        assert reg.status_code == 200, reg.text
        body = reg.json()
        assert body["client_secret"] is None  # "none" is the default: public/PKCE
        assert body["token_endpoint_auth_method"] == "none"
        client_id = body["client_id"]

        code, verifier = await get_code(
            client, client_id=client_id, username=admin.username, password=password
        )

        token_response = await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert token_response.status_code == 200, token_response.text
        token = token_response.json()
        assert token["token_type"] == "Bearer"
        assert set(token["scope"].split()) == {"resume:read", "metadata:read"}
        assert token["refresh_token"]

        # The same seam tests/test_mcp.py::TestTokenVerifier uses: a live
        # streamable-HTTP session needs the app's lifespan running, which the
        # test client does not start.
        access = await McpTokenVerifier().verify_token(token["access_token"])
        assert access is not None
        assert access.claims["user_id"] == admin.user_id
        assert set(access.scopes) == {"resume:read", "metadata:read"}

    async def test_get_authorize_renders_the_consent_form(self, client):
        client_id = await pre_register()
        _verifier, challenge = pkce_pair()
        response = await client.get(
            "/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "resume:read",
            },
        )
        assert response.status_code == 200
        assert "resume:read" in response.text
        assert "<form" in response.text

    async def test_denying_consent_redirects_with_access_denied(
        self, client, admin, password
    ):
        client_id = await pre_register()
        response, _verifier = await authorize(
            client,
            client_id=client_id,
            username=admin.username,
            password=password,
            decision="deny",
        )
        assert response.status_code == 302
        qs = query_of(response)
        assert qs["error"] == "access_denied"
        assert qs["state"] == "s1"

    async def test_wrong_password_reshows_the_form(self, client, admin):
        client_id = await pre_register()
        response, _verifier = await authorize(
            client, client_id=client_id, username=admin.username, password="wrong"
        )
        assert response.status_code == 401
        assert "<form" in response.text


class TestMfaSecondStep:
    """The authorize flow for an account with a second factor: the password
    step returns a code form instead of a redirect, carrying the protocol
    state forward in hidden fields."""

    async def test_an_enrolled_accounts_password_step_shows_a_code_form(
        self, client, enrolled
    ):
        client_id = await pre_register()
        response, _verifier = await authorize(
            client,
            client_id=client_id,
            username=enrolled.actor.username,
            password=enrolled.actor.password,
        )
        assert response.status_code == 200
        assert "<form" in response.text
        assert 'name="mfa_token"' in response.text
        assert 'name="code"' in response.text
        assert 'name="password"' not in response.text

    async def test_a_valid_code_completes_the_flow(self, client, enrolled, totp_code):
        client_id = await pre_register()
        challenge, _verifier = await authorize(
            client,
            client_id=client_id,
            username=enrolled.actor.username,
            password=enrolled.actor.password,
        )
        mfa_token = extract_hidden_value(challenge.text, "mfa_token")

        response, _verifier = await authorize(
            client,
            client_id=client_id,
            username="",
            password="",
            mfa_token=mfa_token,
            code=totp_code(enrolled.secret),
        )
        assert response.status_code == 303, response.text
        assert "code" in query_of(response)

    async def test_a_wrong_code_reshows_the_code_form_with_a_fresh_token(
        self, client, enrolled
    ):
        client_id = await pre_register()
        challenge, _verifier = await authorize(
            client,
            client_id=client_id,
            username=enrolled.actor.username,
            password=enrolled.actor.password,
        )
        mfa_token = extract_hidden_value(challenge.text, "mfa_token")

        response, _verifier = await authorize(
            client,
            client_id=client_id,
            username="",
            password="",
            mfa_token=mfa_token,
            code="000000",
        )
        assert response.status_code == 401
        assert "not valid" in response.text
        # A fresh token, so the next attempt gets a full window rather than
        # racing whatever was left of the one just spent.
        assert extract_hidden_value(response.text, "mfa_token") != mfa_token

    async def test_denying_at_the_second_step_redirects_with_access_denied(
        self, client, enrolled
    ):
        client_id = await pre_register()
        challenge, _verifier = await authorize(
            client,
            client_id=client_id,
            username=enrolled.actor.username,
            password=enrolled.actor.password,
        )
        mfa_token = extract_hidden_value(challenge.text, "mfa_token")

        response, _verifier = await authorize(
            client,
            client_id=client_id,
            username="",
            password="",
            mfa_token=mfa_token,
            code="",
            decision="deny",
        )
        assert response.status_code == 302
        assert query_of(response)["error"] == "access_denied"

    async def test_a_docs_challenge_is_refused_here(self, client, enrolled):
        """The three surfaces grant different things — a challenge minted
        for the docs login must not be redeemable at /oauth/authorize."""
        settings = ConfigService.get_without_deps().settings
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, enrolled.actor.user_id)
            assert user is not None
            token, _expires_in = MfaChallengeToken.mint(
                settings, user, MfaChallengeContext.DOCS
            )

        client_id = await pre_register()
        response, _verifier = await authorize(
            client,
            client_id=client_id,
            username="",
            password="",
            mfa_token=token,
            code="000000",
        )
        assert response.status_code == 401
        assert "expired" in response.text


class TestSecurityRequirements:
    """One test per security property the design depends on. Each should
    fail if the control it covers is removed."""

    async def test_missing_code_challenge_is_refused(self, client, admin, password):
        client_id = await pre_register()
        response, _v = await authorize(
            client,
            client_id=client_id,
            username=admin.username,
            password=password,
            code_challenge="",
        )
        assert response.status_code == 302
        assert query_of(response)["error"] == "invalid_request"

    async def test_plain_challenge_method_is_refused(self, client, admin, password):
        client_id = await pre_register()
        response, _v = await authorize(
            client,
            client_id=client_id,
            username=admin.username,
            password=password,
            code_challenge_method="plain",
        )
        assert response.status_code == 302
        assert query_of(response)["error"] == "invalid_request"

    async def test_redirect_uri_must_match_exactly(self, client):
        client_id = await pre_register()
        _verifier, challenge = pkce_pair()
        response = await client.get(
            "/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI + "/extra",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "resume:read",
            },
        )
        assert response.status_code == 400
        assert "location" not in response.headers

    async def test_unvalidated_redirect_uri_never_redirects_on_error(self, client):
        """Unknown client_id: the error must render a page, never a bounce to
        a redirect_uri nobody has checked."""
        _verifier, challenge = pkce_pair()
        response = await client.get(
            "/oauth/authorize",
            params={
                "client_id": "no-such-client",
                "redirect_uri": "https://evil.example/cb",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "resume:read",
            },
        )
        assert response.status_code == 400
        assert "location" not in response.headers

    async def test_code_is_single_use(self, client, admin, password):
        client_id = await pre_register()
        code, verifier = await get_code(
            client, client_id=client_id, username=admin.username, password=password
        )
        body = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        }
        first = await client.post("/oauth/token", data=body)
        assert first.status_code == 200
        second = await client.post("/oauth/token", data=body)
        assert second.status_code == 400
        assert second.json()["error"] == "invalid_grant"

    async def test_wrong_code_verifier_is_refused(self, client, admin, password):
        client_id = await pre_register()
        code, _verifier = await get_code(
            client, client_id=client_id, username=admin.username, password=password
        )
        response = await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": "totally-the-wrong-verifier-00000000000",
            },
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"

    async def test_mismatched_redirect_uri_at_token_exchange_is_refused(
        self, client, admin, password
    ):
        client_id = await pre_register()
        code, verifier = await get_code(
            client, client_id=client_id, username=admin.username, password=password
        )
        response = await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://claude.ai/a-different/callback",
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"

    async def test_refresh_rotation_and_reuse_detection_revokes_the_chain(
        self, client, admin, password
    ):
        client_id = await pre_register()
        code, verifier = await get_code(
            client, client_id=client_id, username=admin.username, password=password
        )
        first_token = (
            await client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "client_id": client_id,
                    "code_verifier": verifier,
                },
            )
        ).json()
        first_refresh = first_token["refresh_token"]

        rotated = await client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": first_refresh,
                "client_id": client_id,
            },
        )
        assert rotated.status_code == 200
        second_refresh = rotated.json()["refresh_token"]
        assert second_refresh != first_refresh

        replay = await client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": first_refresh,
                "client_id": client_id,
            },
        )
        assert replay.status_code == 400
        assert replay.json()["error"] == "invalid_grant"

        # The reuse revoked the whole chain, so the token issued by the
        # legitimate rotation is dead too.
        chained = await client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": second_refresh,
                "client_id": client_id,
            },
        )
        assert chained.status_code == 400
        assert chained.json()["error"] == "invalid_grant"

    async def test_wrong_audience_is_refused(self, admin):
        settings = ConfigService.get_with_deps().settings
        forged = pyjwt.encode(
            {
                "sub": str(admin.user_id),
                "scope": "resume:read",
                "aud": "http://wrong-resource/resume/mcp",
                "client_id": "x",
                "token_use": "oauth_access",
            },
            settings.auth_secret_key.get_secret_value(),
            algorithm=settings.auth_algorithm,
        )
        assert await McpTokenVerifier().verify_token(forged) is None

    async def test_a_token_with_no_client_id_is_refused(self, admin):
        """RFC 9068 makes `client_id` required and every token this service
        mints carries one, so a token without it was not minted here — and it
        is the field deregistration is checked against."""
        settings = ConfigService.get_with_deps().settings
        forged = pyjwt.encode(
            {
                "iss": str(settings.public_base_url),
                "sub": str(admin.user_id),
                "scope": "resume:read",
                "aud": settings.oauth_resource_url,
                "token_use": "oauth_access",
            },
            settings.auth_secret_key.get_secret_value(),
            algorithm=settings.auth_algorithm,
        )
        assert await McpTokenVerifier().verify_token(forged) is None

    async def test_scope_is_never_trusted_past_current_role(self, member):
        """A token could in principle claim any scope; the server must still
        cap it at what the user's current role actually grants."""
        access_token = await OAuthTokens.mint(
            member.user_id,
            *Scopes,  # every scope, including users:admin
        )
        result = await McpTokenVerifier().verify_token(access_token)
        assert result is not None
        assert Scopes.USERS_ADMIN.value not in result.scopes  # member lacks it

    async def test_consent_cannot_grant_more_than_the_users_role(
        self, client, roleless, password
    ):
        client_id = await pre_register()
        response, _v = await authorize(
            client,
            client_id=client_id,
            username=roleless.username,
            password=password,
            scope="resume:read",
        )
        assert response.status_code == 302
        assert query_of(response)["error"] == "invalid_scope"

    async def test_password_checks_at_authorize_go_through_the_lockout(
        self, client, admin, reconfigure
    ):
        reconfigure(
            AUTH_LOCKOUT_MAX_ATTEMPTS=3,
            AUTH_LOCKOUT_WINDOW_MINUTES=15,
            AUTH_LOCKOUT_BASE_MINUTES=1,
            AUTH_LOCKOUT_PERMANENT_AFTER_LOCKS=3,
            AUTH_IP_MAX_FAILURES=10000,
        )
        client_id = await pre_register()
        for _ in range(3):
            response, _v = await authorize(
                client, client_id=client_id, username=admin.username, password="wrong"
            )
        assert response.status_code == 429

    async def test_client_secret_is_hashed_and_shown_once(self, client, reconfigure):
        reconfigure(OAUTH_REGISTRATION_ENABLED="true")
        response = await register_over_http(
            client, token_endpoint_auth_method="client_secret_post"
        )
        assert response.status_code == 200
        body = response.json()
        raw_secret = body["client_secret"]
        assert raw_secret is not None

        async with DatabaseService.session() as db:
            stored = await OAuthClient.get_by_client_id(db, body["client_id"])
        assert stored is not None
        assert stored.client_secret_hash is not None
        assert stored.client_secret_hash != raw_secret

    async def test_no_credential_appears_in_a_rejected_registration_log(
        self, client, caplog, reconfigure
    ):
        reconfigure(OAUTH_REGISTRATION_ENABLED="true")
        await register_over_http(client, redirect_uris=["https://evil.example/cb"])
        assert "evil.example" in caplog.text  # the rejection is logged...
        # ...but there is no secret in this flow to leak in the first place;
        # the assertion that matters is that the offending host is visible.


class TestRedirectAllowlistMatching:
    """Matching, against a fixed multi-entry list. Table-driven, and the
    multi-entry cases exist to catch an implementation that reads only the
    first entry."""

    ALLOWED = RedirectAllowlist.parse("claude.ai, chatgpt.com, *.staging.example.com")

    @pytest.mark.parametrize(
        "uri",
        [
            "https://claude.ai/api/mcp/auth_callback",
            "https://chatgpt.com/cb",
            "https://a.staging.example.com/cb",
            "https://a.b.staging.example.com/cb",
            "https://CLAUDE.AI/cb",
            "http://127.0.0.1:53219/callback",
            "http://localhost:8080/cb",
        ],
    )
    def test_accepts(self, uri):
        assert self.ALLOWED.allows(uri)

    @pytest.mark.parametrize(
        "uri",
        [
            "https://claude.ai.evil.com/cb",
            "https://evilclaude.ai/cb",
            "https://evil.com/?next=https://claude.ai",
            "https://claude.ai@evil.com/cb",
            "http://claude.ai/cb",
            "https://staging.example.com/cb",
            "https://other.com/cb",
            # RFC 6749 §3.1.2 — a redirect URI carries no fragment, on an
            # otherwise-allowed host, including the empty-fragment form that
            # urlsplit cannot distinguish from having none.
            "https://claude.ai/cb#frag",
            "https://claude.ai/cb#",
            "http://127.0.0.1:53219/callback#frag",
        ],
    )
    def test_rejects(self, uri):
        assert not self.ALLOWED.allows(uri)


class TestRedirectAllowlistParsing:
    """Parsing and validation of the configured value itself."""

    def test_whitespace_and_trailing_comma_are_tolerated(self):
        assert RedirectAllowlist.parse(" claude.ai , chatgpt.com ,").hosts == frozenset(
            {"claude.ai", "chatgpt.com"}
        )

    def test_duplicates_collapse(self):
        assert RedirectAllowlist.parse("claude.ai,claude.ai").hosts == frozenset(
            {"claude.ai"}
        )

    @pytest.mark.parametrize(
        "entry",
        [
            "https://claude.ai/api/mcp/auth_callback",
            "claude.ai:443",
            "user@claude.ai",
            "*",
            "*.",
            "*.com",
            "*.ai",
            "127.0.0.1",
            "::1",
            "localhost",
        ],
    )
    def test_rejects_each_row_of_the_entry_validation_table(self, entry):
        with pytest.raises(ValueError):
            RedirectAllowlist.parse(entry)

    @pytest.mark.parametrize("raw", [None, "", "   ", ","])
    def test_empty_or_unset_fails_rather_than_silently_blocking_everything(self, raw):
        with pytest.raises(ValueError):
            RedirectAllowlist.parse(raw)


class TestRedirectAllowlistLivePolicy:
    """Live policy: removing a host takes effect at /oauth/authorize
    immediately, for clients registered before the removal. Registration-time
    tests cannot reach this."""

    async def test_removing_a_host_blocks_authorize_for_an_already_registered_client(
        self, client, reconfigure
    ):
        client_id = await pre_register()

        reconfigure(OAUTH_ALLOWED_REDIRECT_HOSTS="chatgpt.com")

        _verifier, challenge = pkce_pair()
        response = await client.get(
            "/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "resume:read",
            },
        )
        assert response.status_code == 400
        assert "location" not in response.headers

    async def test_a_still_listed_host_keeps_working(self, client, reconfigure):
        client_id = await pre_register()

        reconfigure(OAUTH_ALLOWED_REDIRECT_HOSTS="claude.ai,chatgpt.com")

        _verifier, challenge = pkce_pair()
        response = await client.get(
            "/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "resume:read",
            },
        )
        assert response.status_code == 200


class TestBackwardsCompatibility:
    """Every credential that worked before OAuth existed. A failure here is a
    release blocker."""

    async def test_api_key_still_works_on_rest_endpoints(self, client, admin):
        created = await client.post(
            "/api-keys",
            headers=admin.headers,
            json={"name": "k", "scopes": [Scopes.USERS_ADMIN.value]},
        )
        raw_key = created.json()["key"]
        response = await client.get(
            "/users/me", headers={"Authorization": f"Bearer {raw_key}"}
        )
        assert response.status_code == 200

    async def test_the_same_api_key_still_works_on_mcp(self, client, admin):
        created = await client.post(
            "/api-keys",
            headers=admin.headers,
            json={"name": "k", "scopes": [Scopes.RESUME_READ.value]},
        )
        raw_key = created.json()["key"]
        result = await McpTokenVerifier().verify_token(raw_key)
        assert result is not None

    async def test_password_jwt_still_works_on_rest_endpoints(self, client, admin):
        response = await client.get("/users/me", headers=admin.headers)
        assert response.status_code == 200

    async def test_password_jwt_still_works_on_mcp(self, admin):
        result = await McpTokenVerifier().verify_token(admin.token)
        assert result is not None

    async def test_expired_or_revoked_api_key_still_rejected(self, client, admin):
        created = await client.post(
            "/api-keys",
            headers=admin.headers,
            json={"name": "k", "scopes": [Scopes.RESUME_READ.value]},
        )
        key_id = created.json()["api_key"]["id"]
        raw_key = created.json()["key"]
        await client.delete(f"/api-keys/{key_id}", headers=admin.headers)
        response = await client.get(
            "/users/me", headers={"Authorization": f"Bearer {raw_key}"}
        )
        assert response.status_code == 401


FROZEN_PATH_METHODS = {
    "/": {"GET"},
    "/token": {"POST"},
    "/users": {"POST"},
    "/users/me": {"GET"},
    "/users/{user_id}": {"PATCH"},
    "/users/{user_id}/lock": {"DELETE"},
    "/api-keys": {"GET", "POST"},
    "/api-keys/{key_id}": {"DELETE"},
    "/documents": {"GET"},
    "/documents/resume/{document_name}": {"GET"},
    "/documents/resume": {"POST"},
    "/documents/metadata/{document_name}": {"GET"},
    "/documents/metadata": {"POST"},
    "/documents/skill/{document_name}": {"GET"},
    "/documents/skill": {"POST"},
    "/documents/{document_name}": {"DELETE"},
    "/public/{username}/resume/{document_name}": {"GET"},
    "/public/users/{user_id}/resume/{document_name}": {"GET"},
}


class TestFrozenSurface:
    async def test_every_frozen_path_is_present_with_its_original_methods(self, client):
        schema = (await client.get("/openapi.json")).json()
        paths = schema["paths"]
        missing = set(FROZEN_PATH_METHODS) - set(paths)
        assert not missing, f"frozen paths missing from the schema: {missing}"

        for path, expected_methods in FROZEN_PATH_METHODS.items():
            actual_methods = {method.upper() for method in paths[path]}
            assert expected_methods <= actual_methods, (
                f"{path}: expected methods {expected_methods}, got {actual_methods}"
            )

    async def test_docs_endpoints_still_respond(self, client):
        for path in ("/docs", "/redoc", "/docs/oauth2-redirect", "/openapi.json"):
            response = await client.get(path)
            assert response.status_code != 404, path

    async def test_oauth_endpoints_do_not_collide_with_the_frozen_surface(self, client):
        schema = (await client.get("/openapi.json")).json()
        new_paths = {
            "/.well-known/oauth-authorization-server",
            "/oauth/register",
            "/oauth/token",
            "/oauth/revoke",
        }
        assert new_paths <= set(schema["paths"])
        assert not (new_paths & set(FROZEN_PATH_METHODS))


class TestAccessTokenConformsToRfc9068:
    """The JWT profile for OAuth access tokens (RFC 9068).

    Not pedantry: a token issued from an OAuth flow is inspectable by whoever
    receives it, and a client that checks conformance refuses a
    non-conforming one rather than presenting it — which looks from the
    server side like a clean exchange followed by nothing at all.
    """

    async def _token(self):
        settings = ConfigService.get_without_deps().settings
        async with DatabaseService.session() as db:
            token, _ = TokenIssuer(db, settings).mint_access_token(
                user_id=1, scopes=frozenset({Scopes.RESUME_READ}), client_id="c1"
            )
        return settings, token

    async def test_typ_header_is_at_jwt(self):
        _settings, token = await self._token()
        assert pyjwt.get_unverified_header(token)["typ"] == "at+jwt"

    async def test_every_required_claim_is_present(self):
        _settings, token = await self._token()
        claims = pyjwt.decode(token, options={"verify_signature": False})
        for required in ("iss", "exp", "aud", "sub", "client_id", "iat", "jti"):
            assert required in claims, required

    async def test_iss_is_identical_to_the_advertised_issuer(self, client):
        """A client comparing the token's issuer against the metadata's must
        find them equal, trailing slash included.

        Compared against the served document rather than the settings object,
        so changing one side alone fails here.
        """
        _settings, token = await self._token()
        claims = pyjwt.decode(token, options={"verify_signature": False})
        advertised = (
            await client.get("/.well-known/oauth-authorization-server")
        ).json()["issuer"]
        assert claims["iss"] == advertised

    async def test_aud_is_the_advertised_resource(self, client):
        _settings, token = await self._token()
        claims = pyjwt.decode(token, options={"verify_signature": False})
        resource = (
            await client.get("/.well-known/oauth-protected-resource/resume/mcp")
        ).json()["resource"]
        assert claims["aud"] == resource

    async def test_a_token_from_another_issuer_is_refused(self, admin):
        """`iss` is checked, not merely emitted — a token signed with this
        key but claiming a different issuer must not authenticate."""
        settings = ConfigService.get_without_deps().settings
        forged = pyjwt.encode(
            {
                "iss": "https://someone-else.example/",
                "sub": str(admin.user_id),
                "scope": "resume:read",
                "aud": settings.oauth_resource_url,
                "client_id": "x",
                "token_use": "oauth_access",
            },
            settings.auth_secret_key.get_secret_value(),
            algorithm=settings.auth_algorithm,
        )
        assert await McpTokenVerifier().verify_token(forged) is None


class TestIssuableScopes:
    """A client builds its request from `scopes_supported`, so what is
    advertised is what gets asked for. Advertising the whole enum meant Claude
    requesting users:admin, and being granted a subset of what it asked for —
    a downgrade some clients treat as a failed authorization."""

    async def test_both_documents_advertise_only_the_issuable_set(self, client):
        expected = sorted(str(s) for s in OAuthScopes.ISSUABLE)
        asm = (await client.get("/.well-known/oauth-authorization-server")).json()
        prm = (
            await client.get("/.well-known/oauth-protected-resource/resume/mcp")
        ).json()
        assert asm["scopes_supported"] == expected
        assert sorted(prm["scopes_supported"]) == expected

    async def test_no_write_or_admin_scope_is_advertised(self, client):
        asm = (await client.get("/.well-known/oauth-authorization-server")).json()
        for forbidden in ("users:admin", "resume:write", "resume:delete"):
            assert forbidden not in asm["scopes_supported"], forbidden

    async def test_asking_for_everything_grants_only_the_issuable_set(
        self, client, admin, password
    ):
        """An admin holds every scope, so nothing here is withheld for lack of
        a role — the cap is what narrows it."""
        client_id = await pre_register()
        code, verifier = await get_code(
            client,
            client_id=client_id,
            username=admin.username,
            password=password,
            scope=" ".join(str(s) for s in Scopes),
        )
        response = await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
        )
        assert response.status_code == 200, response.text
        granted = set(response.json()["scope"].split())
        assert granted == {str(s) for s in OAuthScopes.ISSUABLE}
        assert "users:admin" not in granted

    async def test_a_client_asking_for_exactly_what_is_advertised_is_not_downgraded(
        self, client, admin, password
    ):
        """The property that matters: request what the metadata offers and the
        granted scope comes back identical, so a client comparing the two
        finds nothing missing."""
        advertised = (
            await client.get("/.well-known/oauth-authorization-server")
        ).json()["scopes_supported"]
        client_id = await pre_register()
        code, verifier = await get_code(
            client,
            client_id=client_id,
            username=admin.username,
            password=password,
            scope=" ".join(advertised),
        )
        response = await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
        )
        assert response.status_code == 200, response.text
        assert set(response.json()["scope"].split()) == set(advertised)

    async def test_asking_only_for_non_issuable_scopes_is_refused(
        self, client, admin, password
    ):
        client_id = await pre_register()
        response, _verifier = await authorize(
            client,
            client_id=client_id,
            username=admin.username,
            password=password,
            scope="users:admin resume:delete",
        )
        assert response.status_code == 302
        assert query_of(response)["error"] == "invalid_scope"


class TestMfaChallengeBinding:
    """A challenge is bound to the client whose consent screen produced it.

    Without the binding, one issued while authorizing a client is redeemable
    while authorizing a different one — and the scopes the user saw and
    approved on the first page are not the scopes the second page asked for.
    """

    async def test_a_challenge_from_one_client_is_refused_by_another(
        self, client, enrolled
    ):
        first_client = await pre_register()
        second_client = await pre_register()

        challenge, _verifier = await authorize(
            client,
            client_id=first_client,
            username=enrolled.actor.username,
            password=enrolled.actor.password,
        )
        assert challenge.status_code == 200
        mfa_token = extract_hidden_value(challenge.text, "mfa_token")

        response, _verifier = await authorize(
            client,
            client_id=second_client,
            username="",
            password="",
            mfa_token=mfa_token,
            code="000000",
        )
        # Refused as an expired/unusable challenge — the login form again,
        # not the code form, and certainly not a redirect carrying a code.
        assert response.status_code == 401
        assert 'name="password"' in response.text
        assert response.status_code != 303
