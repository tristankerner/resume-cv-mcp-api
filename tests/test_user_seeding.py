"""New accounts start with the three example documents already in place —
see services/user/document_seeder.py. Applies to every new user, including
the bootstrap admin.
"""

import secrets

from persistence.document import Document
from persistence.user import User
from services.auth.roles import Roles
from services.database.database_service import DatabaseService
from services.user.bootstrap import AdminBootstrapper, BootstrapOutcome

SEEDED_NAMES = {"resume", "metadata", "skill"}


async def register(client, admin, username, password) -> int:
    """`member`, so the new account holds the read scopes needed to read its
    own seeded documents back — registration itself does not depend on any
    role."""
    response = await client.post(
        "/users",
        headers=admin.headers,
        json={
            "username": username,
            "password": password,
            "roles": [Roles.MEMBER.value],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def login(client, username, password) -> dict[str, str]:
    response = await client.post(
        "/token", data={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


class TestSeedingOnRegistration:
    async def test_a_new_user_has_exactly_three_documents(
        self, client, admin, password
    ):
        await register(client, admin, "seeded-user", password)
        headers = await login(client, "seeded-user", password)

        response = await client.get("/documents", headers=headers)
        assert response.status_code == 200
        names = {doc["name"] for doc in response.json()["data"]}
        assert names == SEEDED_NAMES

    async def test_every_seeded_document_is_private(self, client, admin, password):
        await register(client, admin, "seeded-user", password)
        headers = await login(client, "seeded-user", password)

        response = await client.get("/documents", headers=headers)
        assert all(doc["public"] is False for doc in response.json()["data"])

    async def test_seeded_resume_reads_back_and_validates(
        self, client, admin, password
    ):
        await register(client, admin, "seeded-user", password)
        headers = await login(client, "seeded-user", password)

        # Validation happens on the way out: the route's response model is
        # GetDocumentRevisionsResponse[ResumePrivate], so a 200 here is proof
        # the seeded payload matches the schema.
        response = await client.get("/documents/resume/resume", headers=headers)
        assert response.status_code == 200

    async def test_seeded_metadata_reads_back_and_validates(
        self, client, admin, password
    ):
        await register(client, admin, "seeded-user", password)
        headers = await login(client, "seeded-user", password)

        response = await client.get("/documents/metadata/metadata", headers=headers)
        assert response.status_code == 200

    async def test_seeded_skill_reads_back_and_validates(self, client, admin, password):
        await register(client, admin, "seeded-user", password)
        headers = await login(client, "seeded-user", password)

        response = await client.get("/documents/skill/skill", headers=headers)
        assert response.status_code == 200

    async def test_the_public_route_404s_until_published(self, client, admin, password):
        await register(client, admin, "seeded-user", password)
        response = await client.get("/public/seeded-user/resume/resume")
        assert response.status_code == 404

    async def test_publishing_the_seeded_resume_makes_it_public(
        self, client, admin, password
    ):
        await register(client, admin, "seeded-user", password)
        headers = await login(client, "seeded-user", password)

        current = await client.get("/documents/resume/resume", headers=headers)
        data = current.json()["data"][0]["data"]
        response = await client.post(
            "/documents/resume",
            headers=headers,
            json={
                "name": "resume",
                "revision_note": "publish",
                "public": True,
                "data": data,
            },
        )
        assert response.status_code == 200

        published = await client.get("/public/seeded-user/resume/resume")
        assert published.status_code == 200

    async def test_existing_document_fixtures_are_unaffected(
        self, client, admin, stored_resume
    ):
        """conftest's actor fixtures create users through `_create_user`
        (SQL-level, bypassing UserService.register_user), so seeding does not
        apply to them — `admin` still owns exactly the one document
        `stored_resume` wrote."""
        response = await client.get("/documents", headers=admin.headers)
        names = {doc["name"] for doc in response.json()["data"]}
        assert names == {"resume.json"}


class TestSeedingOnBootstrap:
    async def test_the_bootstrap_admin_is_seeded(self, password):
        username = f"bootstrap-seed-{secrets.token_hex(4)}"
        async with DatabaseService.session() as db:
            outcome, created = await AdminBootstrapper(db).ensure_admin(
                username, password
            )
            assert outcome is BootstrapOutcome.CREATED
            assert created == username

            user = await User.get_user_by_username(db, username)
            assert user is not None
            docs = await Document.list_latest(db, user.id)

        names = {doc.name for doc in docs}
        assert names == SEEDED_NAMES
        assert all(doc.public is False for doc in docs)
