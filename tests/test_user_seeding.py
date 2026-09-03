"""New accounts start with the three example documents already in place —
see services/user/document_seeder.py. Applies to every new user, including
the bootstrap admin.
"""

import secrets

import pytest
from sqlalchemy import delete, insert

from persistence.document import Document
from persistence.document_schema import DocumentSchema
from persistence.user import User
from services.auth.roles import Roles
from services.database.database_service import DatabaseService
from services.user.bootstrap import AdminBootstrapper, BootstrapOutcome
from services.user.document_seeder import DocumentSeeder

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


class TestSeedingIsAtomic:
    """A user and their documents arrive together or not at all.

    Seeding writes the rows directly rather than through
    `Document.upsert_document`, which commits per document — going through it
    would commit the account partway through and leave a user holding one or
    two of the three when anything went wrong.
    """

    async def test_a_failure_mid_seed_creates_no_user(
        self, client, admin, password, monkeypatch
    ):
        original = DocumentSeeder.seed

        async def fail_after_the_first(self, db, owner_id):
            await original(self, db, owner_id)
            raise RuntimeError("storage went away mid-seed")

        monkeypatch.setattr(DocumentSeeder, "seed", fail_after_the_first)

        with pytest.raises(RuntimeError):
            await client.post(
                "/users",
                headers=admin.headers,
                json={"username": "half-seeded", "password": password},
            )

        async with DatabaseService.session() as db:
            assert await User.get_user_by_username(db, "half-seeded") is None


class TestSeedingCatalogueGuards:
    """Registration fails loudly, naming the type, when a catalogue example
    is malformed or a type's row is missing — see
    SCHEMA_VERSIONING_PLAN.md Phase 7's "hazard stated plainly": a malformed
    example used to be a build error (DocumentSeeder validated at import);
    now it is a runtime error during registration, so these two guards are
    what stand in for that lost build-time check.

    Both tests mutate `document_schema` inside a session that is never
    committed, so the change is discarded when the session closes — the
    catalogue other tests see is untouched.
    """

    async def test_missing_catalogue_row_raises(self, admin):
        async with DatabaseService.session() as db:
            await db.execute(
                delete(DocumentSchema).where(DocumentSchema.document_type == "resume")
            )
            with pytest.raises(RuntimeError, match="resume"):
                await DocumentSeeder().seed(db, admin.user_id)

    async def test_malformed_catalogue_example_raises(self, admin):
        async with DatabaseService.session() as db:
            await db.execute(
                insert(DocumentSchema).values(
                    version=99,
                    document_type="resume",
                    description="malformed, for this test only",
                    json_schema={},
                    example={"not": "a valid resume"},
                )
            )
            with pytest.raises(RuntimeError, match="resume"):
                await DocumentSeeder().seed(db, admin.user_id)
