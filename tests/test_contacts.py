"""Contacts: CRUD, dedup, company_id filter, ownership isolation."""


class TestCreateContact:
    async def test_creates_a_contact(self, client, admin, company):
        response = await client.post(
            "/contacts",
            headers=admin.headers,
            json={
                "company_id": company["id"],
                "first_name": "Sam",
                "last_name": "Okafor",
                "email": "sam@example.com",
                "rating": 8,
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["first_name"] == "Sam"
        assert body["company_name"] == "Acme Inc"
        assert body["email"] == "sam@example.com"

    async def test_requires_some_identity(self, client, admin, company):
        response = await client.post(
            "/contacts", headers=admin.headers, json={"company_id": company["id"]}
        )
        assert response.status_code == 422

    async def test_company_id_may_be_absent(self, client, admin):
        response = await client.post(
            "/contacts", headers=admin.headers, json={"first_name": "Independent"}
        )
        assert response.status_code == 201
        assert response.json()["company_id"] is None

    async def test_unknown_company_id_is_422(self, client, admin):
        response = await client.post(
            "/contacts",
            headers=admin.headers,
            json={"company_id": 999999, "first_name": "Sam"},
        )
        assert response.status_code == 422

    async def test_rating_out_of_range_is_422(self, client, admin):
        response = await client.post(
            "/contacts",
            headers=admin.headers,
            json={"first_name": "Sam", "rating": 11},
        )
        assert response.status_code == 422

    async def test_rejects_unknown_fields(self, client, admin):
        response = await client.post(
            "/contacts", headers=admin.headers, json={"first_name": "Sam", "nope": 1}
        )
        assert response.status_code == 422

    async def test_requires_a_credential(self, client):
        response = await client.post("/contacts", json={"first_name": "Sam"})
        assert response.status_code == 401

    async def test_email_dedup_is_case_insensitive(self, client, admin):
        first = await client.post(
            "/contacts", headers=admin.headers, json={"email": "Sam@Example.com"}
        )
        assert first.status_code == 201

        response = await client.post(
            "/contacts", headers=admin.headers, json={"email": "sam@example.com"}
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "duplicate_contact"

    async def test_email_dedup_confirmable(self, client, admin):
        await client.post(
            "/contacts", headers=admin.headers, json={"email": "sam@example.com"}
        )
        response = await client.post(
            "/contacts",
            headers=admin.headers,
            json={"email": "sam@example.com", "confirm_create_duplicate": True},
        )
        assert response.status_code == 201

    async def test_name_dedup_scoped_to_same_company(self, client, admin, company):
        await client.post(
            "/contacts",
            headers=admin.headers,
            json={
                "company_id": company["id"],
                "first_name": "Sam",
                "last_name": "Okafor",
            },
        )
        # Same name, no company - different scope, so no collision.
        response = await client.post(
            "/contacts",
            headers=admin.headers,
            json={"first_name": "Sam", "last_name": "Okafor"},
        )
        assert response.status_code == 201

        # Same name, same company - collision.
        response = await client.post(
            "/contacts",
            headers=admin.headers,
            json={
                "company_id": company["id"],
                "first_name": "Sam",
                "last_name": "Okafor",
            },
        )
        assert response.status_code == 409


class TestListContacts:
    async def test_lists_the_callers_contacts(self, client, admin):
        await client.post(
            "/contacts", headers=admin.headers, json={"first_name": "Sam"}
        )
        response = await client.get("/contacts", headers=admin.headers)
        assert response.status_code == 200
        assert response.json()["total"] == 1

    async def test_filters_by_company_id(self, client, admin, company):
        await client.post(
            "/contacts",
            headers=admin.headers,
            json={"company_id": company["id"], "first_name": "Sam"},
        )
        await client.post(
            "/contacts", headers=admin.headers, json={"first_name": "Independent"}
        )
        response = await client.get(
            "/contacts", headers=admin.headers, params={"company_id": company["id"]}
        )
        assert response.json()["total"] == 1
        assert response.json()["data"][0]["first_name"] == "Sam"

    async def test_does_not_list_another_users_contacts(
        self, client, admin, other_owner
    ):
        await client.post(
            "/contacts", headers=admin.headers, json={"first_name": "Sam"}
        )
        response = await client.get("/contacts", headers=other_owner.headers)
        assert response.json()["total"] == 0

    async def test_requires_a_credential(self, client):
        response = await client.get("/contacts")
        assert response.status_code == 401


class TestGetContact:
    async def test_returns_the_contact(self, client, admin):
        created = await client.post(
            "/contacts", headers=admin.headers, json={"first_name": "Sam"}
        )
        response = await client.get(
            f"/contacts/{created.json()['id']}", headers=admin.headers
        )
        assert response.status_code == 200
        assert response.json()["first_name"] == "Sam"

    async def test_unknown_id_is_404(self, client, admin):
        response = await client.get("/contacts/999999", headers=admin.headers)
        assert response.status_code == 404

    async def test_another_users_contact_is_404_not_403(
        self, client, admin, other_owner
    ):
        created = await client.post(
            "/contacts", headers=admin.headers, json={"first_name": "Sam"}
        )
        response = await client.get(
            f"/contacts/{created.json()['id']}", headers=other_owner.headers
        )
        assert response.status_code == 404


class TestUpdateContact:
    async def test_updates_named_fields(self, client, admin):
        created = await client.post(
            "/contacts", headers=admin.headers, json={"first_name": "Sam"}
        )
        response = await client.patch(
            f"/contacts/{created.json()['id']}",
            headers=admin.headers,
            json={"rating": 9},
        )
        assert response.status_code == 200
        assert response.json()["rating"] == 9
        assert response.json()["first_name"] == "Sam"

    async def test_moving_to_another_company(self, client, admin, company):
        created = await client.post(
            "/contacts", headers=admin.headers, json={"first_name": "Sam"}
        )
        response = await client.patch(
            f"/contacts/{created.json()['id']}",
            headers=admin.headers,
            json={"company_id": company["id"]},
        )
        assert response.status_code == 200
        assert response.json()["company_name"] == "Acme Inc"

    async def test_clearing_all_identity_fields_is_422(self, client, admin):
        created = await client.post(
            "/contacts", headers=admin.headers, json={"first_name": "Sam"}
        )
        response = await client.patch(
            f"/contacts/{created.json()['id']}",
            headers=admin.headers,
            json={"first_name": None},
        )
        assert response.status_code == 422

    async def test_another_users_contact_is_404(self, client, admin, other_owner):
        created = await client.post(
            "/contacts", headers=admin.headers, json={"first_name": "Sam"}
        )
        response = await client.patch(
            f"/contacts/{created.json()['id']}",
            headers=other_owner.headers,
            json={"rating": 1},
        )
        assert response.status_code == 404


class TestDeleteContact:
    async def test_deletes_a_contact(self, client, admin):
        created = await client.post(
            "/contacts", headers=admin.headers, json={"first_name": "Sam"}
        )
        response = await client.delete(
            f"/contacts/{created.json()['id']}", headers=admin.headers
        )
        assert response.status_code == 204

        response = await client.get(
            f"/contacts/{created.json()['id']}", headers=admin.headers
        )
        assert response.status_code == 404

    async def test_another_users_contact_is_404(self, client, admin, other_owner):
        created = await client.post(
            "/contacts", headers=admin.headers, json={"first_name": "Sam"}
        )
        response = await client.delete(
            f"/contacts/{created.json()['id']}", headers=other_owner.headers
        )
        assert response.status_code == 404
