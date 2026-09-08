"""GET /tracking/enums."""


class TestGetEnums:
    async def test_returns_all_four_families(self, client, admin):
        response = await client.get("/tracking/enums", headers=admin.headers)
        assert response.status_code == 200
        body = response.json()
        assert len(body["application_statuses"]) == 14
        assert len(body["stack_item_types"]) == 5
        assert len(body["company_relationship_types"]) == 7
        assert len(body["attachment_kinds"]) == 3

    async def test_terminal_flags(self, client, admin):
        response = await client.get("/tracking/enums", headers=admin.headers)
        statuses = {
            s["value"]: s["terminal"] for s in response.json()["application_statuses"]
        }
        assert statuses["submitted"] is False
        assert statuses["rejected"] is True
        assert statuses["ghosted"] is True

    async def test_requires_a_credential(self, client):
        response = await client.get("/tracking/enums")
        assert response.status_code == 401

    async def test_any_one_tracking_read_scope_is_enough(self, client, make_actor):
        from persistence.role_scope import RoleScope
        from services.database.database_service import DatabaseService

        async with DatabaseService.session() as db:
            db.add(RoleScope(role="contacts-only", scope="contacts:read"))
            await db.commit()
        actor = await make_actor("contacts-only-user", ["contacts-only"])
        response = await client.get("/tracking/enums", headers=actor.headers)
        assert response.status_code == 200
