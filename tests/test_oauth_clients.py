"""Admin OAuth client management — /oauth-clients, distinct from the RFC
6749/7591 surface under /oauth/*."""

from sqlalchemy import select

from persistence.oauth_authorization_code import OAuthAuthorizationCode
from persistence.oauth_client import OAuthClient
from persistence.oauth_refresh_token import OAuthRefreshToken
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService
from tests.test_oauth import REDIRECT_URI, get_code

BLOCKED_REDIRECT_URI = "https://evil.example.invalid/callback"


async def narrowed_api_key(client, actor, *scopes: Scopes) -> dict[str, str]:
    created = await client.post(
        "/api-keys",
        headers=actor.headers,
        json={"name": "narrowed", "scopes": [scope.value for scope in scopes]},
    )
    assert created.status_code == 200, created.text
    return {"Authorization": f"Bearer {created.json()['key']}"}


async def create_client(client, actor, **overrides):
    body = {
        "client_name": "Test Client",
        "redirect_uris": [REDIRECT_URI],
        **overrides,
    }
    return await client.post("/oauth-clients", headers=actor.headers, json=body)


class TestCreate:
    async def test_admin_creates_a_client(self, client, admin):
        response = await create_client(client, admin)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["client"]["client_name"] == "Test Client"
        assert body["client"]["redirect_uris"] == [REDIRECT_URI]
        assert body["client"]["confidential"] is True
        assert body["client_secret"]

    async def test_public_client_has_no_secret(self, client, admin):
        response = await create_client(client, admin, public=True)
        body = response.json()
        assert body["client_secret"] is None
        assert body["client"]["confidential"] is False
        assert body["client"]["token_endpoint_auth_method"] == "none"

    async def test_confidential_client_uses_client_secret_post(self, client, admin):
        response = await create_client(client, admin, public=False)
        assert response.json()["client"]["token_endpoint_auth_method"] == (
            "client_secret_post"
        )

    async def test_the_secret_never_appears_in_the_list(self, client, admin):
        created = await create_client(client, admin)
        secret = created.json()["client_secret"]

        listed = await client.get("/oauth-clients", headers=admin.headers)
        assert secret not in listed.text
        assert "client_secret_hash" not in listed.text

    async def test_a_member_is_refused(self, client, member):
        response = await create_client(client, member)
        assert response.status_code == 403

    async def test_an_admin_api_key_is_refused_not_interactive(self, client, admin):
        headers = await narrowed_api_key(client, admin, Scopes.USERS_ADMIN)
        response = await client.post(
            "/oauth-clients",
            headers=headers,
            json={"client_name": "x", "redirect_uris": [REDIRECT_URI]},
        )
        assert response.status_code == 403

    async def test_requires_a_credential(self, client):
        response = await client.post(
            "/oauth-clients",
            json={"client_name": "x", "redirect_uris": [REDIRECT_URI]},
        )
        assert response.status_code == 401

    async def test_a_disallowed_redirect_host_is_refused(self, client, admin):
        response = await create_client(
            client, admin, redirect_uris=[BLOCKED_REDIRECT_URI]
        )
        assert response.status_code == 400
        assert "detail" in response.json()
        assert "error" not in response.json()

    async def test_a_blank_name_is_rejected(self, client, admin):
        response = await create_client(client, admin, client_name="   ")
        assert response.status_code == 422

    async def test_no_redirect_uris_is_rejected(self, client, admin):
        response = await create_client(client, admin, redirect_uris=[])
        assert response.status_code == 422


class TestList:
    async def test_admin_lists_registered_clients(self, client, admin):
        await create_client(client, admin, client_name="First")
        await create_client(client, admin, client_name="Second")

        response = await client.get("/oauth-clients", headers=admin.headers)
        assert response.status_code == 200
        names = {row["client_name"] for row in response.json()["data"]}
        assert {"First", "Second"} <= names

    async def test_a_member_is_refused(self, client, member):
        response = await client.get("/oauth-clients", headers=member.headers)
        assert response.status_code == 403

    async def test_an_admin_api_key_is_refused_not_interactive(self, client, admin):
        headers = await narrowed_api_key(client, admin, Scopes.USERS_ADMIN)
        response = await client.get("/oauth-clients", headers=headers)
        assert response.status_code == 403

    async def test_requires_a_credential(self, client):
        assert (await client.get("/oauth-clients")).status_code == 401


class TestDelete:
    async def test_admin_deletes_a_client(self, client, admin):
        created = await create_client(client, admin)
        client_id = created.json()["client"]["client_id"]

        response = await client.delete(
            f"/oauth-clients/{client_id}", headers=admin.headers
        )
        assert response.status_code == 204

        listed = await client.get("/oauth-clients", headers=admin.headers)
        assert client_id not in [row["client_id"] for row in listed.json()["data"]]

    async def test_a_member_is_refused(self, client, admin, member):
        created = await create_client(client, admin)
        client_id = created.json()["client"]["client_id"]

        response = await client.delete(
            f"/oauth-clients/{client_id}", headers=member.headers
        )
        assert response.status_code == 403

    async def test_an_admin_api_key_is_refused_not_interactive(self, client, admin):
        created = await create_client(client, admin)
        client_id = created.json()["client"]["client_id"]

        headers = await narrowed_api_key(client, admin, Scopes.USERS_ADMIN)
        response = await client.delete(f"/oauth-clients/{client_id}", headers=headers)
        assert response.status_code == 403

    async def test_unknown_client_is_404(self, client, admin):
        response = await client.delete(
            "/oauth-clients/does-not-exist", headers=admin.headers
        )
        assert response.status_code == 404

    async def test_requires_a_credential(self, client, admin):
        created = await create_client(client, admin)
        client_id = created.json()["client"]["client_id"]

        response = await client.delete(f"/oauth-clients/{client_id}")
        assert response.status_code == 401

    async def test_deleting_a_client_removes_its_live_grants(
        self, client, admin, password
    ):
        """A live authorization code and a live refresh token both go with
        the client, and a subsequent token exchange fails."""
        created = await create_client(client, admin, public=True)
        client_id = created.json()["client"]["client_id"]

        # One code left unconsumed.
        live_code, _verifier = await get_code(
            client, client_id=client_id, username=admin.username, password=password
        )

        # A second code, exchanged for a refresh token.
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
        refresh_token = token_response.json()["refresh_token"]

        async with DatabaseService.session() as db:
            codes = (
                (
                    await db.execute(
                        select(OAuthAuthorizationCode).where(
                            OAuthAuthorizationCode.client_id == client_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            tokens = (
                (
                    await db.execute(
                        select(OAuthRefreshToken).where(
                            OAuthRefreshToken.client_id == client_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert codes  # the unconsumed one from get_code above
        assert tokens

        response = await client.delete(
            f"/oauth-clients/{client_id}", headers=admin.headers
        )
        assert response.status_code == 204

        async with DatabaseService.session() as db:
            assert await OAuthClient.get_by_client_id(db, client_id) is None
            remaining_codes = (
                (
                    await db.execute(
                        select(OAuthAuthorizationCode).where(
                            OAuthAuthorizationCode.client_id == client_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            remaining_tokens = (
                (
                    await db.execute(
                        select(OAuthRefreshToken).where(
                            OAuthRefreshToken.client_id == client_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert remaining_codes == []
        assert remaining_tokens == []

        # The token exchange this client would otherwise have satisfied now
        # fails: the client itself is gone.
        refresh_response = await client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
            },
        )
        assert refresh_response.status_code == 401
        assert refresh_response.json()["error"] == "invalid_client"

        exchange_response = await client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": live_code,
                "redirect_uri": REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": _verifier,
            },
        )
        assert exchange_response.status_code == 401
        assert exchange_response.json()["error"] == "invalid_client"
