"""Companies: CRUD, dedup, relationships, stack items, ownership isolation."""

from persistence.application import Application
from services.database.database_service import DatabaseService


async def _insert_application(user_id: int, company_id: int) -> int:
    """Applications aren't reachable through the API yet at this point in the
    build - inserted directly so the "company has applications" 409 can be
    tested before `ApplicationService` exists."""
    async with DatabaseService.session() as db:
        application = Application(
            user_id=user_id, company_id=company_id, status="submitted"
        )
        db.add(application)
        await db.commit()
        return application.id


class TestCreateCompany:
    async def test_creates_a_company(self, client, admin):
        response = await client.post(
            "/companies",
            headers=admin.headers,
            json={"name": "Plaid", "website": "https://plaid.com/"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["name"] == "Plaid"
        assert body["website"] == "https://plaid.com/"
        assert body["application_count"] == 0
        assert body["contact_count"] == 0

    async def test_rejects_unknown_fields(self, client, admin):
        response = await client.post(
            "/companies", headers=admin.headers, json={"name": "X", "nope": 1}
        )
        assert response.status_code == 422

    async def test_website_must_be_http_or_https(self, client, admin):
        response = await client.post(
            "/companies",
            headers=admin.headers,
            json={"name": "X", "website": "ftp://example.com"},
        )
        assert response.status_code == 422

    async def test_requires_a_credential(self, client):
        response = await client.post("/companies", json={"name": "X"})
        assert response.status_code == 401

    async def test_exact_duplicate_is_blocked(self, client, admin, company):
        response = await client.post(
            "/companies", headers=admin.headers, json={"name": "Acme Inc"}
        )
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["code"] == "duplicate_company"
        assert detail["candidates"][0]["match"] == "exact"

    async def test_exact_duplicate_cannot_be_forced_through(
        self, client, admin, company
    ):
        """The unique index is absolute: confirm_create_duplicate overrides a
        near-match warning, not an exact collision."""
        response = await client.post(
            "/companies",
            headers=admin.headers,
            json={"name": "Acme Inc", "confirm_create_duplicate": True},
        )
        assert response.status_code == 409

    async def test_fuzzy_duplicate_is_blocked_then_confirmable(
        self, client, admin, company
    ):
        response = await client.post(
            "/companies", headers=admin.headers, json={"name": "Acmee Inc"}
        )
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["code"] == "duplicate_company"
        assert len(detail["candidates"]) >= 1

        response = await client.post(
            "/companies",
            headers=admin.headers,
            json={"name": "Acmee Inc", "confirm_create_duplicate": True},
        )
        assert response.status_code == 201

    async def test_different_users_can_each_have_a_company_with_the_same_name(
        self, client, admin, other_owner, company
    ):
        response = await client.post(
            "/companies", headers=other_owner.headers, json={"name": "Acme Inc"}
        )
        assert response.status_code == 201


class TestListCompanies:
    async def test_lists_the_callers_companies(self, client, admin, company):
        response = await client.get("/companies", headers=admin.headers)
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["data"][0]["id"] == company["id"]

    async def test_does_not_list_another_users_companies(
        self, client, admin, other_owner, company
    ):
        response = await client.get("/companies", headers=other_owner.headers)
        assert response.json()["total"] == 0

    async def test_query_filters_by_name(self, client, admin, company):
        response = await client.get(
            "/companies", headers=admin.headers, params={"query": "nonexistent"}
        )
        assert response.json()["total"] == 0

        response = await client.get(
            "/companies", headers=admin.headers, params={"query": "acme"}
        )
        assert response.json()["total"] == 1

    async def test_requires_a_credential(self, client):
        response = await client.get("/companies")
        assert response.status_code == 401


class TestGetCompany:
    async def test_returns_detail_shape(self, client, admin, company):
        response = await client.get(
            f"/companies/{company['id']}", headers=admin.headers
        )
        assert response.status_code == 200
        body = response.json()
        assert body["relationships"] == []
        assert body["stack"] == []
        assert body["contacts"] == []
        assert body["recent_applications"] == []

    async def test_unknown_id_is_404(self, client, admin):
        response = await client.get("/companies/999999", headers=admin.headers)
        assert response.status_code == 404

    async def test_another_users_company_is_404_not_403(
        self, client, other_owner, company
    ):
        response = await client.get(
            f"/companies/{company['id']}", headers=other_owner.headers
        )
        assert response.status_code == 404


class TestUpdateCompany:
    async def test_updates_named_fields(self, client, admin, company):
        response = await client.patch(
            f"/companies/{company['id']}",
            headers=admin.headers,
            json={"description": "A fintech company."},
        )
        assert response.status_code == 200
        assert response.json()["description"] == "A fintech company."
        # website untouched - PATCH is partial.
        assert response.json()["website"] is None

    async def test_explicit_null_clears_a_field(self, client, admin, company):
        await client.patch(
            f"/companies/{company['id']}",
            headers=admin.headers,
            json={"website": "https://acme.example"},
        )
        response = await client.patch(
            f"/companies/{company['id']}",
            headers=admin.headers,
            json={"website": None},
        )
        assert response.status_code == 200
        assert response.json()["website"] is None

    async def test_renaming_to_an_existing_name_is_blocked(
        self, client, admin, company
    ):
        other = await client.post(
            "/companies", headers=admin.headers, json={"name": "Other Co"}
        )
        response = await client.patch(
            f"/companies/{other.json()['id']}",
            headers=admin.headers,
            json={"name": "Acme Inc"},
        )
        assert response.status_code == 409

    async def test_renaming_to_its_own_current_name_is_a_no_op(
        self, client, admin, company
    ):
        response = await client.patch(
            f"/companies/{company['id']}",
            headers=admin.headers,
            json={"name": "Acme Inc"},
        )
        assert response.status_code == 200

    async def test_another_users_company_is_404(self, client, other_owner, company):
        response = await client.patch(
            f"/companies/{company['id']}",
            headers=other_owner.headers,
            json={"description": "hijacked"},
        )
        assert response.status_code == 404


class TestDeleteCompany:
    async def test_deletes_a_company(self, client, admin, company):
        response = await client.delete(
            f"/companies/{company['id']}", headers=admin.headers
        )
        assert response.status_code == 204

        response = await client.get(
            f"/companies/{company['id']}", headers=admin.headers
        )
        assert response.status_code == 404

    async def test_blocked_when_applications_reference_it(self, client, admin, company):
        await _insert_application(admin.user_id, company["id"])
        response = await client.delete(
            f"/companies/{company['id']}", headers=admin.headers
        )
        assert response.status_code == 409
        assert "1 application" in response.json()["detail"]

    async def test_deleting_nulls_out_contacts_company_id(self, client, admin, company):
        contact = await client.post(
            "/contacts",
            headers=admin.headers,
            json={"company_id": company["id"], "first_name": "Sam"},
        )
        assert contact.status_code == 201
        response = await client.delete(
            f"/companies/{company['id']}", headers=admin.headers
        )
        assert response.status_code == 204

        refetched = await client.get(
            f"/contacts/{contact.json()['id']}", headers=admin.headers
        )
        assert refetched.json()["company_id"] is None

    async def test_another_users_company_is_404(self, client, other_owner, company):
        response = await client.delete(
            f"/companies/{company['id']}", headers=other_owner.headers
        )
        assert response.status_code == 404


class TestCompanyRelationships:
    async def test_creates_a_relationship(self, client, admin, company):
        parent = await client.post(
            "/companies", headers=admin.headers, json={"name": "Globex Corp"}
        )
        response = await client.post(
            f"/companies/{company['id']}/relationships",
            headers=admin.headers,
            json={"to_company_id": parent.json()["id"], "type": "child_of"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["from_company_id"] == company["id"]
        assert body["to_company_name"] == "Globex Corp"

    async def test_self_relationship_is_422(self, client, admin, company):
        response = await client.post(
            f"/companies/{company['id']}/relationships",
            headers=admin.headers,
            json={"to_company_id": company["id"], "type": "child_of"},
        )
        assert response.status_code == 422

    async def test_target_must_be_the_callers_own_company(
        self, client, admin, other_owner, company
    ):
        their_company = await client.post(
            "/companies", headers=other_owner.headers, json={"name": "Other Co"}
        )
        response = await client.post(
            f"/companies/{company['id']}/relationships",
            headers=admin.headers,
            json={"to_company_id": their_company.json()["id"], "type": "child_of"},
        )
        assert response.status_code == 422

    async def test_duplicate_edge_is_409(self, client, admin, company):
        parent = await client.post(
            "/companies", headers=admin.headers, json={"name": "Globex Corp"}
        )
        body = {"to_company_id": parent.json()["id"], "type": "child_of"}
        first = await client.post(
            f"/companies/{company['id']}/relationships",
            headers=admin.headers,
            json=body,
        )
        assert first.status_code == 201
        second = await client.post(
            f"/companies/{company['id']}/relationships",
            headers=admin.headers,
            json=body,
        )
        assert second.status_code == 409

    async def test_update_and_delete(self, client, admin, company):
        parent = await client.post(
            "/companies", headers=admin.headers, json={"name": "Globex Corp"}
        )
        created = await client.post(
            f"/companies/{company['id']}/relationships",
            headers=admin.headers,
            json={"to_company_id": parent.json()["id"], "type": "child_of"},
        )
        relationship_id = created.json()["id"]

        updated = await client.patch(
            f"/company-relationships/{relationship_id}",
            headers=admin.headers,
            json={"note": "acquired 2024"},
        )
        assert updated.status_code == 200
        assert updated.json()["note"] == "acquired 2024"

        deleted = await client.delete(
            f"/company-relationships/{relationship_id}", headers=admin.headers
        )
        assert deleted.status_code == 204

    async def test_create_on_another_users_company_is_404(
        self, client, other_owner, company
    ):
        response = await client.post(
            f"/companies/{company['id']}/relationships",
            headers=other_owner.headers,
            json={"to_company_id": company["id"] + 1, "type": "child_of"},
        )
        assert response.status_code == 404

    async def test_patch_on_another_users_relationship_is_404(
        self, client, admin, other_owner, company
    ):
        parent = await client.post(
            "/companies", headers=admin.headers, json={"name": "Globex Corp"}
        )
        created = await client.post(
            f"/companies/{company['id']}/relationships",
            headers=admin.headers,
            json={"to_company_id": parent.json()["id"], "type": "child_of"},
        )
        response = await client.patch(
            f"/company-relationships/{created.json()['id']}",
            headers=other_owner.headers,
            json={"note": "hijacked"},
        )
        assert response.status_code == 404

    async def test_delete_on_another_users_relationship_is_404(
        self, client, admin, other_owner, company
    ):
        parent = await client.post(
            "/companies", headers=admin.headers, json={"name": "Globex Corp"}
        )
        created = await client.post(
            f"/companies/{company['id']}/relationships",
            headers=admin.headers,
            json={"to_company_id": parent.json()["id"], "type": "child_of"},
        )
        response = await client.delete(
            f"/company-relationships/{created.json()['id']}",
            headers=other_owner.headers,
        )
        assert response.status_code == 404


class TestCompanyStack:
    async def test_creates_a_stack_item(self, client, admin, company):
        response = await client.post(
            f"/companies/{company['id']}/stack",
            headers=admin.headers,
            json={"name": "Go", "type": "programming_language"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["name"] == "Go"

    async def test_lists_stack_items(self, client, admin, company):
        await client.post(
            f"/companies/{company['id']}/stack",
            headers=admin.headers,
            json={"name": "Go", "type": "programming_language"},
        )
        response = await client.get(
            f"/companies/{company['id']}/stack", headers=admin.headers
        )
        assert response.status_code == 200
        assert len(response.json()["data"]) == 1

    async def test_exact_duplicate_stack_item_is_blocked(self, client, admin, company):
        """Case-insensitive equality after normalization - "go" and "Go" are
        the same item, so the unique index would refuse this regardless of
        confirm_create_duplicate."""
        await client.post(
            f"/companies/{company['id']}/stack",
            headers=admin.headers,
            json={"name": "Go", "type": "programming_language"},
        )
        response = await client.post(
            f"/companies/{company['id']}/stack",
            headers=admin.headers,
            json={
                "name": "go",
                "type": "programming_language",
                "confirm_create_duplicate": True,
            },
        )
        assert response.status_code == 409

    async def test_fuzzy_duplicate_stack_item_is_blocked_then_confirmable(
        self, client, admin, company
    ):
        await client.post(
            f"/companies/{company['id']}/stack",
            headers=admin.headers,
            json={"name": "PostgreSQL", "type": "programming_language"},
        )
        response = await client.post(
            f"/companies/{company['id']}/stack",
            headers=admin.headers,
            json={"name": "Postgres", "type": "programming_language"},
        )
        assert response.status_code == 409
        response = await client.post(
            f"/companies/{company['id']}/stack",
            headers=admin.headers,
            json={
                "name": "Postgres",
                "type": "programming_language",
                "confirm_create_duplicate": True,
            },
        )
        assert response.status_code == 201

    async def test_update_and_delete(self, client, admin, company):
        created = await client.post(
            f"/companies/{company['id']}/stack",
            headers=admin.headers,
            json={"name": "Go", "type": "programming_language"},
        )
        item_id = created.json()["id"]

        updated = await client.patch(
            f"/company-stack/{item_id}",
            headers=admin.headers,
            json={"description": "Backend services."},
        )
        assert updated.status_code == 200
        assert updated.json()["description"] == "Backend services."

        deleted = await client.delete(
            f"/company-stack/{item_id}", headers=admin.headers
        )
        assert deleted.status_code == 204

    async def test_list_on_another_users_company_is_404(
        self, client, other_owner, company
    ):
        response = await client.get(
            f"/companies/{company['id']}/stack", headers=other_owner.headers
        )
        assert response.status_code == 404

    async def test_create_on_another_users_company_is_404(
        self, client, other_owner, company
    ):
        response = await client.post(
            f"/companies/{company['id']}/stack",
            headers=other_owner.headers,
            json={"name": "Go", "type": "programming_language"},
        )
        assert response.status_code == 404

    async def test_patch_on_another_users_item_is_404(
        self, client, admin, other_owner, company
    ):
        created = await client.post(
            f"/companies/{company['id']}/stack",
            headers=admin.headers,
            json={"name": "Go", "type": "programming_language"},
        )
        response = await client.patch(
            f"/company-stack/{created.json()['id']}",
            headers=other_owner.headers,
            json={"description": "hijacked"},
        )
        assert response.status_code == 404

    async def test_delete_on_another_users_item_is_404(
        self, client, admin, other_owner, company
    ):
        created = await client.post(
            f"/companies/{company['id']}/stack",
            headers=admin.headers,
            json={"name": "Go", "type": "programming_language"},
        )
        response = await client.delete(
            f"/company-stack/{created.json()['id']}", headers=other_owner.headers
        )
        assert response.status_code == 404


class TestWebsiteSchemeIsRestricted:
    """Same reasoning as `applications.url` - see TestUrlSchemeIsRestricted in
    tests/test_applications.py."""

    async def test_create_rejects_a_script_url(self, client, admin):
        response = await client.post(
            "/companies",
            headers=admin.headers,
            json={"name": "Evil Corp", "website": "javascript:alert(1)"},
        )
        assert response.status_code == 422, response.text

    async def test_update_rejects_a_script_url(self, client, admin, company):
        response = await client.patch(
            f"/companies/{company['id']}",
            headers=admin.headers,
            json={"website": "javascript:alert(1)"},
        )
        assert response.status_code == 422, response.text
