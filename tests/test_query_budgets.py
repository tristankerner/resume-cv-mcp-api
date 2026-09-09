"""Query-count regression tests.

See `QUERY_PERFORMANCE_PLAN.md`. The central assertion is invariance: a list
endpoint's statement count must not grow with the number of rows on the page.
An endpoint that still carries an N+1 gets `pytest.mark.xfail(strict=True)`
rather than a skip, so it starts failing the moment someone fixes it without
updating this file - the corresponding phase then flips the mark off as its
acceptance check. Every N+1 known when this suite was first written was
fixed in the same phase, so none is marked today.

Every count below includes the one `SELECT users.*` `AuthService` issues per
request to authenticate the bearer token.
"""

import secrets

from httpx import AsyncClient

from tests.support.query_recorder import QueryRecorder


async def _make_company(client: AsyncClient, admin, name: str) -> int:
    """`name` gets a random suffix - the dedup heuristic is fuzzy enough
    that "Acme 5" and "Acme 25" collide, which is correct product behaviour
    and not something these tests want to fight."""
    unique_name = f"{name} {secrets.token_hex(4)}"
    response = await client.post(
        "/companies", headers=admin.headers, json={"name": unique_name}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _make_application(
    client: AsyncClient, admin, company_id: int, job_title: str
) -> int:
    """`job_title` gets a random suffix - same dedup-collision reasoning as
    `_make_company`."""
    unique_title = f"{job_title} {secrets.token_hex(4)}"
    response = await client.post(
        "/applications",
        headers=admin.headers,
        json={"company_id": company_id, "job_title": unique_title},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _make_contact(
    client: AsyncClient, admin, company_id: int, first_name: str
) -> int:
    """`first_name` gets a random suffix - same dedup-collision reasoning as
    `_make_company`."""
    unique_name = f"{first_name} {secrets.token_hex(4)}"
    response = await client.post(
        "/contacts",
        headers=admin.headers,
        json={"company_id": company_id, "first_name": unique_name},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _make_event(
    client: AsyncClient, admin, application_id: int, contact_id: int, status: str
) -> None:
    response = await client.post(
        f"/applications/{application_id}/events",
        headers=admin.headers,
        json={"status": status, "contact_id": contact_id},
    )
    assert response.status_code == 201, response.text


async def _seed_applications(client: AsyncClient, admin, count: int) -> int:
    company_id = await _make_company(client, admin, f"Acme {count}")
    for i in range(count):
        await _make_application(client, admin, company_id, f"Role {i}")
    return company_id


async def _seed_contacts(client: AsyncClient, admin, count: int) -> int:
    company_id = await _make_company(client, admin, f"Contact Co {count}")
    for i in range(count):
        await _make_contact(client, admin, company_id, f"Person {i}")
    return company_id


class TestListInvariance:
    """Endpoints with no N+1 today: statement count is flat in page size."""

    async def test_applications_list(self, client, admin):
        await _seed_applications(client, admin, 5)
        with QueryRecorder() as small:
            response = await client.get("/applications", headers=admin.headers)
        assert response.status_code == 200, response.text

        await _seed_applications(client, admin, 25)
        with QueryRecorder() as large:
            response = await client.get(
                "/applications", headers=admin.headers, params={"limit": 200}
            )
        assert response.status_code == 200, response.text

        assert small.count() == large.count(), (small.tally(), large.tally())
        # 1 auth + rows(with windowed total, joined company name, correlated
        # event/attachment counts). `job_code_match_counts` is skipped here:
        # none of the seeded rows carry a job code, so its guard clause never
        # issues a query. Matches QUERY_PERFORMANCE_PLAN.md's Phase 4 target.
        assert small.count() == 2

    async def test_companies_list(self, client, admin):
        for i in range(5):
            await _make_company(client, admin, f"Small Co {i}")
        with QueryRecorder() as small:
            response = await client.get("/companies", headers=admin.headers)
        assert response.status_code == 200, response.text

        for i in range(25):
            await _make_company(client, admin, f"Large Co {i}")
        with QueryRecorder() as large:
            response = await client.get(
                "/companies", headers=admin.headers, params={"limit": 200}
            )
        assert response.status_code == 200, response.text

        assert small.count() == large.count(), (small.tally(), large.tally())
        # 1 auth + rows(with windowed total, correlated application/contact
        # counts). Matches QUERY_PERFORMANCE_PLAN.md's Phase 4 target.
        assert small.count() == 2

    async def test_audit_list(self, client, admin):
        await _seed_applications(client, admin, 5)
        with QueryRecorder() as small:
            response = await client.get("/audit", headers=admin.headers)
        assert response.status_code == 200, response.text

        await _seed_applications(client, admin, 25)
        with QueryRecorder() as large:
            response = await client.get(
                "/audit", headers=admin.headers, params={"limit": 200}
            )
        assert response.status_code == 200, response.text

        assert small.count() == large.count(), (small.tally(), large.tally())
        # 1 auth + rows(with windowed total).
        assert small.count() == 2

    async def test_contacts_list(self, client, admin):
        """Fixed by QUERY_PERFORMANCE_PLAN.md Phase 2: `Lookup.map` replaced
        a `Company.get` per contact row."""
        await _seed_contacts(client, admin, 5)
        with QueryRecorder() as small:
            response = await client.get("/contacts", headers=admin.headers)
        assert response.status_code == 200, response.text

        await _seed_contacts(client, admin, 25)
        with QueryRecorder() as large:
            response = await client.get(
                "/contacts", headers=admin.headers, params={"limit": 200}
            )
        assert response.status_code == 200, response.text

        assert small.count() == large.count(), (small.tally(), large.tally())
        # 1 auth + rows(with windowed total) + names_for.
        assert small.count() == 3

    async def test_application_detail(self, client, admin):
        """Fixed by QUERY_PERFORMANCE_PLAN.md Phase 2: `Lookup.map` replaced
        a `Contact.get` per event row."""
        company_id = await _make_company(client, admin, "Event Co")
        contact_id = await _make_contact(client, admin, company_id, "Recruiter")

        small_app_id = await _make_application(client, admin, company_id, "Small Role")
        for i in range(5):
            await _make_event(client, admin, small_app_id, contact_id, "screening")
        with QueryRecorder() as small:
            response = await client.get(
                f"/applications/{small_app_id}", headers=admin.headers
            )
        assert response.status_code == 200, response.text

        large_app_id = await _make_application(client, admin, company_id, "Large Role")
        for i in range(25):
            await _make_event(client, admin, large_app_id, contact_id, "screening")
        with QueryRecorder() as large:
            response = await client.get(
                f"/applications/{large_app_id}", headers=admin.headers
            )
        assert response.status_code == 200, response.text

        assert small.count() == large.count(), (small.tally(), large.tally())
        assert small.count() == 6

    async def test_application_events_list(self, client, admin):
        """Fixed by QUERY_PERFORMANCE_PLAN.md Phase 2: `Lookup.map` replaced
        a `Contact.get` per event row."""
        company_id = await _make_company(client, admin, "Event List Co")
        contact_id = await _make_contact(client, admin, company_id, "Recruiter")

        small_app_id = await _make_application(client, admin, company_id, "Small Role")
        for i in range(5):
            await _make_event(client, admin, small_app_id, contact_id, "screening")
        with QueryRecorder() as small:
            response = await client.get(
                f"/applications/{small_app_id}/events", headers=admin.headers
            )
        assert response.status_code == 200, response.text

        large_app_id = await _make_application(client, admin, company_id, "Large Role")
        for i in range(25):
            await _make_event(client, admin, large_app_id, contact_id, "screening")
        with QueryRecorder() as large:
            response = await client.get(
                f"/applications/{large_app_id}/events", headers=admin.headers
            )
        assert response.status_code == 200, response.text

        assert small.count() == large.count(), (small.tally(), large.tally())
        assert small.count() == 4

    async def test_company_detail_relationships(self, client, admin):
        """Fixed by QUERY_PERFORMANCE_PLAN.md Phase 2: `Company.names_for`
        replaced a `Company.get` per related company."""
        company_id = await _make_company(client, admin, "Hub Co")

        small_other_ids = [
            await _make_company(client, admin, f"Small Related {i}") for i in range(3)
        ]
        for other_id in small_other_ids:
            response = await client.post(
                f"/companies/{company_id}/relationships",
                headers=admin.headers,
                json={"to_company_id": other_id, "type": "partner_of"},
            )
            assert response.status_code == 201, response.text
        with QueryRecorder() as small:
            response = await client.get(
                f"/companies/{company_id}", headers=admin.headers
            )
        assert response.status_code == 200, response.text

        large_other_ids = [
            await _make_company(client, admin, f"Large Related {i}") for i in range(15)
        ]
        for other_id in large_other_ids:
            response = await client.post(
                f"/companies/{company_id}/relationships",
                headers=admin.headers,
                json={"to_company_id": other_id, "type": "partner_of"},
            )
            assert response.status_code == 201, response.text
        with QueryRecorder() as large:
            response = await client.get(
                f"/companies/{company_id}", headers=admin.headers
            )
        assert response.status_code == 200, response.text

        assert small.count() == large.count(), (small.tally(), large.tally())
        # One fewer than before Phase 3: `Contact.search` (used for this
        # company's contact list) collapsed its count-then-rows pair into one
        # windowed query.
        assert small.count() == 9


class TestOverFetch:
    """`ApplicationSummary` never carries `job_description` - see
    QUERY_PERFORMANCE_PLAN.md Defect B. Phase 1 tightens this with a
    `raiseload` assertion at the persistence layer; this is the
    response-shape half of the guarantee, which does not depend on Phase 1
    landing."""

    async def test_applications_list_excludes_job_description(self, client, admin):
        company_id = await _make_company(client, admin, "Heavy Co")
        heavy_description = "x" * 50_000
        response = await client.post(
            "/applications",
            headers=admin.headers,
            json={
                "company_id": company_id,
                "job_title": "Heavy Role",
                "job_description": heavy_description,
            },
        )
        assert response.status_code == 201, response.text

        response = await client.get("/applications", headers=admin.headers)
        assert response.status_code == 200, response.text
        assert heavy_description not in response.text
        assert "job_description" not in response.json()["data"][0]
