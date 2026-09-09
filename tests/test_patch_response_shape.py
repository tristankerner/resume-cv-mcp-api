"""PATCH responses on applications, companies and contacts return the exact
row shape their list endpoint does - see FEATURE_EXPANSION_PLAN.md section
7.1 and API contract 9.5. A client splices the PATCH response straight into
the row it edited without a refetch; this is the property that makes that
safe, and nothing else pins it."""


class TestApplicationPatchMatchesListRow:
    async def test_patch_response_equals_list_row_shape(
        self, client, admin, application
    ):
        patched = await client.patch(
            f"/applications/{application['id']}",
            headers=admin.headers,
            json={"job_title": "Staff Engineer"},
        )
        assert patched.status_code == 200, patched.text

        listing = await client.get("/applications", headers=admin.headers)
        assert listing.status_code == 200, listing.text
        row = next(r for r in listing.json()["data"] if r["id"] == application["id"])

        assert set(patched.json().keys()) == set(row.keys())
        assert patched.json() == row


class TestCompanyPatchMatchesListRow:
    async def test_patch_response_equals_list_row_shape(self, client, admin, company):
        patched = await client.patch(
            f"/companies/{company['id']}",
            headers=admin.headers,
            json={"description": "Updated description."},
        )
        assert patched.status_code == 200, patched.text

        listing = await client.get("/companies", headers=admin.headers)
        assert listing.status_code == 200, listing.text
        row = next(r for r in listing.json()["data"] if r["id"] == company["id"])

        assert set(patched.json().keys()) == set(row.keys())
        assert patched.json() == row


class TestContactPatchMatchesListRow:
    async def test_patch_response_equals_list_row_shape(self, client, admin, company):
        created = await client.post(
            "/contacts",
            headers=admin.headers,
            json={"company_id": company["id"], "first_name": "Dana"},
        )
        assert created.status_code == 201, created.text

        patched = await client.patch(
            f"/contacts/{created.json()['id']}",
            headers=admin.headers,
            json={"last_name": "Reyes"},
        )
        assert patched.status_code == 200, patched.text

        listing = await client.get("/contacts", headers=admin.headers)
        assert listing.status_code == 200, listing.text
        row = next(r for r in listing.json()["data"] if r["id"] == created.json()["id"])

        assert set(patched.json().keys()) == set(row.keys())
        assert patched.json() == row
