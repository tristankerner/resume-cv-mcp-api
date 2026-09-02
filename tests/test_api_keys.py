"""API key minting, verification, narrowing, revocation and expiry."""

import json
from datetime import timedelta

import pytest

from persistence.api_key import ApiKey
from persistence.base import Clock
from persistence.user import User
from services.auth.api_keys import ApiKeyToken
from services.auth.principal import CredentialKind
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService
from tests.test_auth import authenticate


async def mint(client, actor, **overrides) -> dict:
    body = {"name": "a-key", "scopes": [Scopes.RESUME_READ.value], **overrides}
    response = await client.post("/api-keys", headers=actor.headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


class TestKeyFormat:
    def test_generate_returns_key_prefix_and_hash(self):
        key, prefix, key_hash = ApiKeyToken.generate()
        assert key == f"{ApiKeyToken.SCHEME}_{prefix}_{key.split('_')[2]}"
        assert len(key_hash) == 64

    def test_generated_keys_are_unique(self):
        assert len({ApiKeyToken.generate()[0] for _ in range(50)}) == 50

    def test_secret_is_not_recoverable_from_the_hash(self):
        key, _, key_hash = ApiKeyToken.generate()
        assert key.split("_")[2] not in key_hash

    def test_parse_round_trips(self):
        key, prefix, _ = ApiKeyToken.generate()
        assert ApiKeyToken.parse(key) == (prefix, key.split("_")[2])

    @pytest.mark.parametrize(
        "token",
        ["rsm_only-two", "rsm_a_b_c", "other_a_b", "rsm__secret", "rsm_prefix_", ""],
    )
    def test_parse_rejects_malformed(self, token):
        assert ApiKeyToken.parse(token) is None

    def test_looks_like_an_api_key(self):
        assert ApiKeyToken.looks_like(ApiKeyToken.generate()[0])
        assert not ApiKeyToken.looks_like("eyJhbGciOi.some.jwt")

    def test_matches_is_true_only_for_the_right_secret(self):
        key, _, key_hash = ApiKeyToken.generate()
        assert ApiKeyToken.matches(key.split("_")[2], key_hash)
        assert not ApiKeyToken.matches("wrong", key_hash)


class TestMinting:
    async def test_mints_for_self_by_default(self, client, admin):
        created = await mint(client, admin)
        assert created["api_key"]["user_id"] == admin.user_id
        assert created["key"].startswith(ApiKeyToken.SCHEME + "_")

    async def test_response_carries_the_secret_exactly_once(self, client, admin):
        created = await mint(client, admin)
        assert "key" not in created["api_key"]
        listed = await client.get("/api-keys", headers=admin.headers)
        assert created["key"] not in json.dumps(listed.json())

    async def test_only_the_hash_is_stored(self, client, admin):
        created = await mint(client, admin)
        secret = created["key"].split("_")[2]
        async with DatabaseService.session() as db:
            row = await ApiKey.get_by_id(db, created["api_key"]["id"])
        assert row is not None
        assert secret not in json.dumps([row.key_hash, row.prefix, row.name])
        assert row.key_hash == ApiKeyToken.hash_secret(secret)

    async def test_a_user_id_field_is_rejected(self, client, admin, member):
        """Keys are strictly self-service now: naming someone else's user_id
        — the shape the old admin-on-behalf path took — is an unknown field,
        not a request the server tries to honour."""
        response = await client.post(
            "/api-keys",
            headers=admin.headers,
            json={
                "name": "k",
                "scopes": [Scopes.RESUME_READ.value],
                "user_id": member.user_id,
            },
        )
        assert response.status_code == 422

    async def test_cannot_grant_scopes_the_owner_lacks(self, client, member):
        response = await client.post(
            "/api-keys",
            headers=member.headers,
            json={"name": "k", "scopes": [Scopes.USERS_ADMIN.value]},
        )
        assert response.status_code == 403
        assert Scopes.USERS_ADMIN.value in response.json()["detail"]

    @pytest.mark.parametrize(
        "body",
        [
            {"name": "", "scopes": ["resume:read"]},
            {"name": "   ", "scopes": ["resume:read"]},
            {"name": "k", "scopes": []},
            {"name": "k", "scopes": ["not-a-scope"]},
            {"name": "k", "scopes": ["resume:read"], "expires_in_days": 0},
            {"name": "k", "scopes": ["resume:read"], "expires_in_days": -1},
        ],
    )
    async def test_invalid_requests_are_rejected(self, client, admin, body):
        response = await client.post("/api-keys", headers=admin.headers, json=body)
        assert response.status_code == 422

    async def test_name_is_trimmed(self, client, admin):
        created = await mint(client, admin, name="  spaced  ")
        assert created["api_key"]["name"] == "spaced"

    async def test_no_expiry_by_default(self, client, admin):
        assert (await mint(client, admin))["api_key"]["expires_at"] is None

    async def test_expiry_is_set_when_requested(self, client, admin):
        created = await mint(client, admin, expires_in_days=30)
        assert created["api_key"]["expires_at"] is not None


class TestUsingKeys:
    async def test_key_authenticates(self, client, admin, stored_resume):
        key = (await mint(client, admin))["key"]
        response = await client.get("/documents/resume/resume.json", headers=auth(key))
        assert response.status_code == 200

    async def test_principal_records_the_key(self, client, admin):
        created = await mint(client, admin)
        principal = await authenticate(created["key"])
        assert principal.credential is CredentialKind.API_KEY
        assert principal.api_key_id == created["api_key"]["id"]
        assert principal.username == admin.username

    async def test_key_is_limited_to_its_scopes(self, client, admin, resume_payload):
        key = (await mint(client, admin, scopes=[Scopes.SKILL_READ.value]))["key"]
        response = await client.post(
            "/documents/resume",
            headers=auth(key),
            json={"name": "resume.json", "revision_note": "v", "data": resume_payload},
        )
        assert response.status_code == 403

    async def test_key_narrowed_below_its_owner(self, client, admin, stored_resume):
        """The owner is an admin, but this key only reads the skill document."""
        key = (await mint(client, admin, scopes=[Scopes.SKILL_READ.value]))["key"]
        response = await client.get("/documents/resume/resume.json", headers=auth(key))
        assert response.status_code == 403

    async def test_key_scopes_shrink_when_the_owner_loses_a_role(
        self, client, admin, member, stored_resume
    ):
        key = (await mint(client, member))["key"]
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, member.user_id)
            assert user is not None
            user.roles = []
            await db.commit()

        response = await client.get("/documents/resume/resume.json", headers=auth(key))
        assert response.status_code == 403

    async def test_key_dies_with_its_owner(self, client, admin, member, stored_resume):
        key = (await mint(client, member))["key"]
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, member.user_id)
            assert user is not None
            user.active = False
            await db.commit()

        response = await client.get("/documents/resume/resume.json", headers=auth(key))
        assert response.status_code == 401

    async def test_last_used_is_recorded(self, client, admin):
        created = await mint(client, admin)
        assert created["api_key"]["last_used_at"] is None

        await client.get("/users/me", headers=auth(created["key"]))
        listed = await client.get("/api-keys", headers=admin.headers)
        assert listed.json()["data"][0]["last_used_at"] is not None

    async def test_last_used_writes_are_throttled(self, client, admin):
        """Recording use on every request would be a write per call."""
        created = await mint(client, admin)
        await client.get("/users/me", headers=auth(created["key"]))

        async with DatabaseService.session() as db:
            row = await ApiKey.get_by_id(db, created["api_key"]["id"])
            assert row is not None
            first = row.last_used_at

        await client.get("/users/me", headers=auth(created["key"]))
        async with DatabaseService.session() as db:
            row = await ApiKey.get_by_id(db, created["api_key"]["id"])
            assert row is not None
            second = row.last_used_at

        assert first == second

    async def test_last_used_updates_once_the_window_passes(self, client, admin):
        created = await mint(client, admin)
        key_id = created["api_key"]["id"]
        await client.get("/users/me", headers=auth(created["key"]))

        async with DatabaseService.session() as db:
            row = await ApiKey.get_by_id(db, key_id)
            assert row is not None
            assert row.last_used_at is not None  # set by the request above
            stale = row.last_used_at - timedelta(hours=1)
            row.last_used_at = stale
            await db.commit()

        await client.get("/users/me", headers=auth(created["key"]))
        async with DatabaseService.session() as db:
            row = await ApiKey.get_by_id(db, key_id)
            assert row is not None
            assert row.last_used_at is not None
            assert row.last_used_at > stale


class TestRejectedKeys:
    async def test_unknown_prefix(self, client):
        _, _, _ = ApiKeyToken.generate()
        response = await client.get(
            "/users/me", headers=auth(f"{ApiKeyToken.SCHEME}_deadbeef_{'0' * 64}")
        )
        assert response.status_code == 401

    async def test_right_prefix_wrong_secret(self, client, admin):
        created = await mint(client, admin)
        prefix = created["api_key"]["prefix"]
        response = await client.get(
            "/users/me", headers=auth(f"{ApiKeyToken.SCHEME}_{prefix}_{'0' * 64}")
        )
        assert response.status_code == 401

    async def test_malformed_key(self, client):
        assert (
            await client.get("/users/me", headers=auth(f"{ApiKeyToken.SCHEME}_short"))
        ).status_code == 401

    async def test_expired_key(self, client, admin, stored_resume):
        created = await mint(client, admin, expires_in_days=1)
        async with DatabaseService.session() as db:
            row = await ApiKey.get_by_id(db, created["api_key"]["id"])
            assert row is not None
            row.expires_at = Clock.utcnow() - timedelta(seconds=1)
            await db.commit()

        response = await client.get(
            "/documents/resume/resume.json", headers=auth(created["key"])
        )
        assert response.status_code == 401

    async def test_key_valid_right_up_to_expiry(self, client, admin, stored_resume):
        created = await mint(client, admin, expires_in_days=1)
        response = await client.get(
            "/documents/resume/resume.json", headers=auth(created["key"])
        )
        assert response.status_code == 200


class TestKeyManagementRequiresLogin:
    """A key that can mint keys makes revocation meaningless."""

    async def test_key_cannot_mint(self, client, admin):
        key = (await mint(client, admin))["key"]
        response = await client.post(
            "/api-keys",
            headers=auth(key),
            json={"name": "child", "scopes": [Scopes.SKILL_READ.value]},
        )
        assert response.status_code == 403

    async def test_key_cannot_revoke(self, client, admin):
        created = await mint(client, admin)
        response = await client.delete(
            f"/api-keys/{created['api_key']['id']}", headers=auth(created["key"])
        )
        assert response.status_code == 403

    async def test_key_may_still_list(self, client, admin):
        """Reading metadata is harmless and useful for a client checking itself."""
        created = await mint(client, admin)
        response = await client.get("/api-keys", headers=auth(created["key"]))
        assert response.status_code == 200


class TestListing:
    async def test_lists_only_your_own(self, client, admin, member):
        await mint(client, admin, name="mine")
        await mint(client, member, name="theirs")

        names = [
            k["name"]
            for k in (await client.get("/api-keys", headers=admin.headers)).json()[
                "data"
            ]
        ]
        assert names == ["mine"]

    async def test_a_user_id_query_param_is_ignored(self, client, admin, member):
        """No admin-on-behalf listing — the route takes no such parameter, so
        one arriving on the query string is simply unused."""
        await mint(client, admin, name="mine")
        response = await client.get(
            f"/api-keys?user_id={member.user_id}", headers=admin.headers
        )
        assert [k["name"] for k in response.json()["data"]] == ["mine"]

    async def test_empty_when_none_minted(self, client, admin):
        assert (await client.get("/api-keys", headers=admin.headers)).json() == {
            "data": []
        }

    async def test_requires_a_credential(self, client):
        assert (await client.get("/api-keys")).status_code == 401


class TestRevocation:
    async def test_revoked_key_stops_working(self, client, admin, stored_resume):
        created = await mint(client, admin)
        await client.delete(
            f"/api-keys/{created['api_key']['id']}", headers=admin.headers
        )

        response = await client.get(
            "/documents/resume/resume.json", headers=auth(created["key"])
        )
        assert response.status_code == 401

    async def test_row_is_kept_for_audit(self, client, admin):
        created = await mint(client, admin)
        response = await client.delete(
            f"/api-keys/{created['api_key']['id']}", headers=admin.headers
        )
        assert response.json()["revoked_at"] is not None

        listed = await client.get("/api-keys", headers=admin.headers)
        assert len(listed.json()["data"]) == 1

    async def test_revoking_twice_is_idempotent(self, client, admin):
        created = await mint(client, admin)
        first = await client.delete(
            f"/api-keys/{created['api_key']['id']}", headers=admin.headers
        )
        second = await client.delete(
            f"/api-keys/{created['api_key']['id']}", headers=admin.headers
        )
        assert first.json()["revoked_at"] == second.json()["revoked_at"]

    async def test_unknown_key(self, client, admin):
        assert (
            await client.delete("/api-keys/999999", headers=admin.headers)
        ).status_code == 404

    async def test_cannot_revoke_someone_elses(self, client, admin, member):
        created = await mint(client, admin)
        response = await client.delete(
            f"/api-keys/{created['api_key']['id']}", headers=member.headers
        )
        # 404, not 403: whose key this is isn't the caller's business.
        assert response.status_code == 404

    async def test_admin_cannot_revoke_a_members_key(self, client, admin, member):
        """No admin-on-behalf escape hatch — revocation is self-service only."""
        created = await mint(client, member)
        response = await client.delete(
            f"/api-keys/{created['api_key']['id']}", headers=admin.headers
        )
        assert response.status_code == 404
