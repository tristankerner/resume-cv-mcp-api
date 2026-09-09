"""Correctness coverage for the Phase 4 rewrite of `Application.search` and
`Company.search`.

See `QUERY_PERFORMANCE_PLAN.md` Phase 4. `tests/test_query_budgets.py`
covers the round-trip count; this file covers that the correlated
`RelatedCount` columns and the still-grouped `job_code_match_count`
follow-up produce the same numbers a per-row lookup would have.
"""


async def test_list_reports_job_code_match_count_for_duplicates(client, admin, company):
    first = await client.post(
        "/applications",
        headers=admin.headers,
        json={
            "company_id": company["id"],
            "job_title": "Engineer",
            "job_code": "REQ-12345",
        },
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        "/applications",
        headers=admin.headers,
        json={
            "company_id": company["id"],
            "job_title": "Senior Engineer",
            "job_code": "REQ-12345",
            "confirm_create_duplicate": True,
        },
    )
    assert second.status_code == 201, second.text

    listed = await client.get("/applications", headers=admin.headers)
    assert listed.status_code == 200, listed.text
    counts = {row["id"]: row["job_code_match_count"] for row in listed.json()["data"]}
    assert counts[first.json()["id"]] == 1
    assert counts[second.json()["id"]] == 1


async def test_list_reports_zero_job_code_match_count_without_a_code(
    client, admin, company
):
    response = await client.post(
        "/applications",
        headers=admin.headers,
        json={"company_id": company["id"], "job_title": "Engineer"},
    )
    assert response.status_code == 201, response.text

    listed = await client.get("/applications", headers=admin.headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"][0]["job_code_match_count"] == 0


async def test_list_reports_event_and_attachment_counts_per_row(client, admin, company):
    response = await client.post(
        "/applications",
        headers=admin.headers,
        json={"company_id": company["id"], "job_title": "Engineer"},
    )
    assert response.status_code == 201, response.text
    application_id = response.json()["id"]

    for _ in range(3):
        event = await client.post(
            f"/applications/{application_id}/events",
            headers=admin.headers,
            json={"status": "screening"},
        )
        assert event.status_code == 201, event.text

    listed = await client.get("/applications", headers=admin.headers)
    assert listed.status_code == 200, listed.text
    row = listed.json()["data"][0]
    assert row["event_count"] == 3
    assert row["attachment_count"] == 0


async def test_company_list_reports_application_and_contact_counts(client, admin):
    response = await client.post(
        "/companies", headers=admin.headers, json={"name": "Satellite Co"}
    )
    assert response.status_code == 201, response.text
    company_id = response.json()["id"]

    for i in range(2):
        created = await client.post(
            "/applications",
            headers=admin.headers,
            json={"company_id": company_id, "job_title": f"Role {i}"},
        )
        assert created.status_code == 201, created.text

    contact = await client.post(
        "/contacts",
        headers=admin.headers,
        json={"company_id": company_id, "first_name": "Recruiter"},
    )
    assert contact.status_code == 201, contact.text

    listed = await client.get("/companies", headers=admin.headers)
    assert listed.status_code == 200, listed.text
    row = next(row for row in listed.json()["data"] if row["id"] == company_id)
    assert row["application_count"] == 2
    assert row["contact_count"] == 1
