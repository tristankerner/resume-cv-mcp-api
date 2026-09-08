"""Applications: CRUD, filters, pagination, derived status, document
references, ownership isolation."""


class TestCreateApplication:
    async def test_creates_an_application(self, client, admin, company):
        response = await client.post(
            "/applications",
            headers=admin.headers,
            json={"company_id": company["id"], "job_title": "Software Engineer"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["company_id"] == company["id"]
        assert body["status"] == "submitted"
        assert body["status_label"] == "Submitted"
        assert body["event_count"] == 0
        assert body["attachment_count"] == 0

    async def test_unknown_company_id_is_422(self, client, admin):
        response = await client.post(
            "/applications", headers=admin.headers, json={"company_id": 999999}
        )
        assert response.status_code == 422

    async def test_rejects_unknown_fields(self, client, admin, company):
        response = await client.post(
            "/applications",
            headers=admin.headers,
            json={"company_id": company["id"], "nope": 1},
        )
        assert response.status_code == 422

    async def test_requires_a_credential(self, client, company):
        response = await client.post(
            "/applications", json={"company_id": company["id"]}
        )
        assert response.status_code == 401

    async def test_half_stated_document_reference_is_422(self, client, admin, company):
        response = await client.post(
            "/applications",
            headers=admin.headers,
            json={"company_id": company["id"], "resume_document_name": "resume.json"},
        )
        assert response.status_code == 422

    async def test_document_reference_must_belong_to_caller(
        self, client, admin, company, stored_resume
    ):
        response = await client.post(
            "/applications",
            headers=admin.headers,
            json={
                "company_id": company["id"],
                "resume_document_name": "does-not-exist.json",
                "resume_revision_id": 1,
            },
        )
        assert response.status_code == 422

    async def test_document_reference_type_must_match_slot(
        self, client, admin, company, stored_resume
    ):
        """A resume document put in the metadata slot is a type mismatch."""
        response = await client.post(
            "/applications",
            headers=admin.headers,
            json={
                "company_id": company["id"],
                "metadata_document_name": stored_resume["name"],
                "metadata_revision_id": stored_resume["revision_id"],
            },
        )
        assert response.status_code == 422

    async def test_valid_document_reference_is_accepted(
        self, client, admin, company, stored_resume
    ):
        response = await client.post(
            "/applications",
            headers=admin.headers,
            json={
                "company_id": company["id"],
                "resume_document_name": stored_resume["name"],
                "resume_revision_id": stored_resume["revision_id"],
            },
        )
        assert response.status_code == 201, response.text

    async def test_duplicate_application_is_blocked_then_confirmable(
        self, client, admin, company
    ):
        body = {
            "company_id": company["id"],
            "job_title": "Software Engineer",
            "date_submitted": "2026-06-01",
        }
        first = await client.post("/applications", headers=admin.headers, json=body)
        assert first.status_code == 201

        second = await client.post("/applications", headers=admin.headers, json=body)
        assert second.status_code == 409
        assert second.json()["detail"]["code"] == "duplicate_application"

        third = await client.post(
            "/applications",
            headers=admin.headers,
            json={**body, "confirm_create_duplicate": True},
        )
        assert third.status_code == 201

    async def test_seniority_variant_is_flagged_as_duplicate(
        self, client, admin, company
    ):
        await client.post(
            "/applications",
            headers=admin.headers,
            json={
                "company_id": company["id"],
                "job_title": "Senior Software Engineer",
                "date_submitted": "2026-06-01",
            },
        )
        response = await client.post(
            "/applications",
            headers=admin.headers,
            json={
                "company_id": company["id"],
                "job_title": "Software Engineer",
                "date_submitted": "2026-06-05",
            },
        )
        assert response.status_code == 409

    async def test_far_apart_dates_do_not_collide(self, client, admin, company):
        await client.post(
            "/applications",
            headers=admin.headers,
            json={
                "company_id": company["id"],
                "job_title": "Software Engineer",
                "date_submitted": "2026-01-01",
            },
        )
        response = await client.post(
            "/applications",
            headers=admin.headers,
            json={
                "company_id": company["id"],
                "job_title": "Software Engineer",
                "date_submitted": "2026-06-01",
            },
        )
        assert response.status_code == 201


class TestListApplications:
    async def test_lists_the_callers_applications(self, client, admin, application):
        response = await client.get("/applications", headers=admin.headers)
        assert response.status_code == 200
        assert response.json()["total"] == 1

    async def test_does_not_list_another_users_applications(
        self, client, other_owner, application
    ):
        response = await client.get("/applications", headers=other_owner.headers)
        assert response.json()["total"] == 0

    async def test_filters_by_company_id(self, client, admin, company, application):
        response = await client.get(
            "/applications", headers=admin.headers, params={"company_id": company["id"]}
        )
        assert response.json()["total"] == 1
        response = await client.get(
            "/applications", headers=admin.headers, params={"company_id": 999999}
        )
        assert response.json()["total"] == 0

    async def test_filters_by_status_repeatable(self, client, admin, application):
        response = await client.get(
            "/applications",
            headers=admin.headers,
            params=[("status", "rejected"), ("status", "submitted")],
        )
        assert response.json()["total"] == 1
        response = await client.get(
            "/applications", headers=admin.headers, params={"status": "rejected"}
        )
        assert response.json()["total"] == 0

    async def test_filters_by_query_over_job_title(self, client, admin, application):
        response = await client.get(
            "/applications", headers=admin.headers, params={"query": "engineer"}
        )
        assert response.json()["total"] == 1
        response = await client.get(
            "/applications", headers=admin.headers, params={"query": "nonexistent"}
        )
        assert response.json()["total"] == 0

    async def test_pagination(self, client, admin, company):
        for i in range(3):
            await client.post(
                "/applications",
                headers=admin.headers,
                json={"company_id": company["id"], "job_title": f"Role {i}"},
            )
        response = await client.get(
            "/applications", headers=admin.headers, params={"limit": 2, "offset": 0}
        )
        assert response.json()["total"] == 3
        assert len(response.json()["data"]) == 2

        response = await client.get(
            "/applications", headers=admin.headers, params={"limit": 2, "offset": 2}
        )
        assert len(response.json()["data"]) == 1

    async def test_requires_a_credential(self, client):
        response = await client.get("/applications")
        assert response.status_code == 401


class TestGetApplication:
    async def test_returns_detail_shape(self, client, admin, application):
        response = await client.get(
            f"/applications/{application['id']}", headers=admin.headers
        )
        assert response.status_code == 200
        body = response.json()
        assert body["events"] == []
        assert body["attachments"] == []
        assert body["resume_document"] is None

    async def test_unknown_id_is_404(self, client, admin):
        response = await client.get("/applications/999999", headers=admin.headers)
        assert response.status_code == 404

    async def test_another_users_application_is_404_not_403(
        self, client, other_owner, application
    ):
        response = await client.get(
            f"/applications/{application['id']}", headers=other_owner.headers
        )
        assert response.status_code == 404


class TestUpdateApplication:
    async def test_updates_named_fields(self, client, admin, application):
        response = await client.patch(
            f"/applications/{application['id']}",
            headers=admin.headers,
            json={"source": "LinkedIn"},
        )
        assert response.status_code == 200
        assert response.json()["source"] == "LinkedIn"

    async def test_status_is_rejected(self, client, admin, application):
        response = await client.patch(
            f"/applications/{application['id']}",
            headers=admin.headers,
            json={"status": "rejected"},
        )
        assert response.status_code == 422

    async def test_another_users_application_is_404(
        self, client, other_owner, application
    ):
        response = await client.patch(
            f"/applications/{application['id']}",
            headers=other_owner.headers,
            json={"source": "hijacked"},
        )
        assert response.status_code == 404


class TestDeleteApplication:
    async def test_deletes_an_application_and_its_events(
        self, client, admin, application, application_event
    ):
        response = await client.delete(
            f"/applications/{application['id']}", headers=admin.headers
        )
        assert response.status_code == 204
        response = await client.get(
            f"/applications/{application['id']}", headers=admin.headers
        )
        assert response.status_code == 404

    async def test_another_users_application_is_404(
        self, client, other_owner, application
    ):
        response = await client.delete(
            f"/applications/{application['id']}", headers=other_owner.headers
        )
        assert response.status_code == 404


class TestApplicationEvents:
    async def test_create_requires_content(self, client, admin, application):
        response = await client.post(
            f"/applications/{application['id']}/events",
            headers=admin.headers,
            json={},
        )
        assert response.status_code == 422

    async def test_note_only_event(self, client, admin, application):
        response = await client.post(
            f"/applications/{application['id']}/events",
            headers=admin.headers,
            json={"description": "Left a voicemail."},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["event"]["status"] is None
        assert body["application_status"] == "submitted"

    async def test_status_event_recomputes_application_status(
        self, client, admin, application
    ):
        response = await client.post(
            f"/applications/{application['id']}/events",
            headers=admin.headers,
            json={"status": "rejected", "occurred_at": "2026-06-04T00:00:00"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["application_status"] == "rejected"
        assert body["application_status_label"] == "Rejected"

        detail = await client.get(
            f"/applications/{application['id']}", headers=admin.headers
        )
        assert detail.json()["status"] == "rejected"

    async def test_most_recent_occurred_at_wins_not_creation_order(
        self, client, admin, application
    ):
        await client.post(
            f"/applications/{application['id']}/events",
            headers=admin.headers,
            json={"status": "rejected", "occurred_at": "2026-06-10T00:00:00"},
        )
        response = await client.post(
            f"/applications/{application['id']}/events",
            headers=admin.headers,
            json={"status": "screening", "occurred_at": "2026-06-01T00:00:00"},
        )
        # The screening event happened earlier, so status stays "rejected" -
        # the most recent occurred_at wins, not creation order.
        assert response.json()["application_status"] == "rejected"

    async def test_deleting_the_deciding_event_falls_back_to_submitted(
        self, client, admin, application
    ):
        created = await client.post(
            f"/applications/{application['id']}/events",
            headers=admin.headers,
            json={"status": "rejected"},
        )
        event_id = created.json()["event"]["id"]

        response = await client.delete(
            f"/application-events/{event_id}", headers=admin.headers
        )
        assert response.status_code == 200
        assert response.json()["application_status"] == "submitted"
        assert response.json()["event"] is None

    async def test_updating_an_events_status_recomputes(
        self, client, admin, application
    ):
        created = await client.post(
            f"/applications/{application['id']}/events",
            headers=admin.headers,
            json={"status": "screening"},
        )
        event_id = created.json()["event"]["id"]
        response = await client.patch(
            f"/application-events/{event_id}",
            headers=admin.headers,
            json={"status": "rejected"},
        )
        assert response.status_code == 200
        assert response.json()["application_status"] == "rejected"

    async def test_unknown_contact_id_is_422(self, client, admin, application):
        response = await client.post(
            f"/applications/{application['id']}/events",
            headers=admin.headers,
            json={"description": "note", "contact_id": 999999},
        )
        assert response.status_code == 422

    async def test_list_events_newest_first(self, client, admin, application):
        await client.post(
            f"/applications/{application['id']}/events",
            headers=admin.headers,
            json={"description": "first", "occurred_at": "2026-01-01T00:00:00"},
        )
        await client.post(
            f"/applications/{application['id']}/events",
            headers=admin.headers,
            json={"description": "second", "occurred_at": "2026-06-01T00:00:00"},
        )
        response = await client.get(
            f"/applications/{application['id']}/events", headers=admin.headers
        )
        descriptions = [event["description"] for event in response.json()["data"]]
        assert descriptions == ["second", "first"]

    async def test_events_on_another_users_application_is_404(
        self, client, other_owner, application
    ):
        response = await client.get(
            f"/applications/{application['id']}/events", headers=other_owner.headers
        )
        assert response.status_code == 404

    async def test_patch_on_another_users_event_is_404(
        self, client, admin, other_owner, application_event
    ):
        response = await client.patch(
            f"/application-events/{application_event['event']['id']}",
            headers=other_owner.headers,
            json={"description": "hijacked"},
        )
        assert response.status_code == 404

    async def test_delete_on_another_users_event_is_404(
        self, client, admin, other_owner, application_event
    ):
        response = await client.delete(
            f"/application-events/{application_event['event']['id']}",
            headers=other_owner.headers,
        )
        assert response.status_code == 404


class TestUrlSchemeIsRestricted:
    """`url` is rendered as an `<a href>` by the browser client, and tracking
    rows are writable over MCP by an OAuth connector - so a stored
    `javascript:` URL is script execution in the origin holding the session
    token, written by something other than the person who clicks it."""

    async def test_create_rejects_a_script_url(self, client, admin, company):
        response = await client.post(
            "/applications",
            headers=admin.headers,
            json={
                "company_id": company["id"],
                "url": "javascript:alert(document.cookie)",
            },
        )
        assert response.status_code == 422, response.text

    async def test_update_rejects_a_script_url(self, client, admin, application):
        response = await client.patch(
            f"/applications/{application['id']}",
            headers=admin.headers,
            json={"url": "javascript:alert(1)"},
        )
        assert response.status_code == 422, response.text

    async def test_create_rejects_a_data_url(self, client, admin, company):
        response = await client.post(
            "/applications",
            headers=admin.headers,
            json={
                "company_id": company["id"],
                "url": "data:text/html;base64,PHNjcmlwdD4=",
            },
        )
        assert response.status_code == 422, response.text

    async def test_http_and_https_are_accepted(self, client, admin, company):
        for url in ("http://example.test/jobs/1", "https://example.test/jobs/2"):
            response = await client.post(
                "/applications",
                headers=admin.headers,
                json={
                    "company_id": company["id"],
                    "url": url,
                    "job_title": url,
                    "confirm_create_duplicate": True,
                },
            )
            assert response.status_code == 201, response.text
            assert response.json()["url"] == url


class TestJobCode:
    """A job code identifies the same requisition arriving more than once,
    which is the case the field exists for: two recruiters, two companies,
    one job."""

    @staticmethod
    async def _create(client, admin, company_id, **fields):
        body = {"company_id": company_id, "confirm_create_duplicate": True, **fields}
        return await client.post("/applications", headers=admin.headers, json=body)

    async def test_stored_and_returned(self, client, admin, company):
        created = await self._create(
            client, admin, company["id"], job_title="Engineer", job_code="REQ-12345"
        )
        assert created.status_code == 201, created.text
        assert created.json()["job_code"] == "REQ-12345"

        detail = await client.get(
            f"/applications/{created.json()['id']}", headers=admin.headers
        )
        assert detail.json()["job_code"] == "REQ-12345"

    async def test_a_second_recruiter_with_the_same_code_is_refused(
        self, client, admin, company
    ):
        agency = await client.post(
            "/companies", headers=admin.headers, json={"name": "Robert Half"}
        )
        first = await self._create(
            client, admin, company["id"], job_title="Engineer", job_code="REQ-12345"
        )
        assert first.status_code == 201, first.text

        # Different company, different title spelling - still the same job.
        second = await client.post(
            "/applications",
            headers=admin.headers,
            json={
                "company_id": agency.json()["id"],
                "job_title": "Senior Engineer",
                "job_code": "req 12345",
            },
        )
        assert second.status_code == 409, second.text
        detail = second.json()["detail"]
        assert detail["code"] == "duplicate_application"
        assert "job code" in detail["message"]
        assert [c["id"] for c in detail["candidates"]] == [first.json()["id"]]
        assert "Robert Half" not in detail["candidates"][0]["hint"]

    async def test_the_override_records_it_separately(self, client, admin, company):
        first = await self._create(
            client, admin, company["id"], job_title="Engineer", job_code="REQ-12345"
        )
        second = await client.post(
            "/applications",
            headers=admin.headers,
            json={
                "company_id": company["id"],
                "job_title": "Engineer",
                "job_code": "REQ-12345",
                "confirm_create_duplicate": True,
            },
        )
        assert second.status_code == 201, second.text
        assert second.json()["job_code_match_count"] == 1

        refreshed = await client.get(
            f"/applications/{first.json()['id']}", headers=admin.headers
        )
        assert refreshed.json()["job_code_match_count"] == 1
        related = refreshed.json()["related_by_job_code"]
        assert [row["id"] for row in related] == [second.json()["id"]]

    async def test_a_unique_code_relates_to_nothing(self, client, admin, company):
        created = await self._create(
            client, admin, company["id"], job_title="Engineer", job_code="REQ-999"
        )
        detail = await client.get(
            f"/applications/{created.json()['id']}", headers=admin.headers
        )
        assert detail.json()["job_code_match_count"] == 0
        assert detail.json()["related_by_job_code"] == []

    async def test_filter_matches_regardless_of_spelling(self, client, admin, company):
        await self._create(
            client, admin, company["id"], job_title="A", job_code="REQ-12345"
        )
        await self._create(
            client, admin, company["id"], job_title="B", job_code="OTHER-1"
        )

        found = await client.get(
            "/applications", headers=admin.headers, params={"job_code": "req 12345"}
        )
        assert found.status_code == 200, found.text
        assert found.json()["total"] == 1
        assert found.json()["data"][0]["job_title"] == "A"

    async def test_filtering_by_an_unmatchable_code_returns_nothing(
        self, client, admin, company
    ):
        """A code too short to be stored as a key must filter to nothing, not
        silently drop the filter and return everything."""
        await self._create(
            client, admin, company["id"], job_title="A", job_code="REQ-12345"
        )
        found = await client.get(
            "/applications", headers=admin.headers, params={"job_code": "A2"}
        )
        assert found.status_code == 200, found.text
        assert found.json()["total"] == 0

    async def test_a_code_can_be_cleared(self, client, admin, company):
        created = await self._create(
            client, admin, company["id"], job_title="Engineer", job_code="REQ-12345"
        )
        cleared = await client.patch(
            f"/applications/{created.json()['id']}",
            headers=admin.headers,
            json={"job_code": None},
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["job_code"] is None

        found = await client.get(
            "/applications", headers=admin.headers, params={"job_code": "REQ-12345"}
        )
        assert found.json()["total"] == 0

    async def test_another_users_code_is_never_matched(
        self, client, admin, other_owner, company
    ):
        their_company = await client.post(
            "/companies", headers=other_owner.headers, json={"name": "Theirs"}
        )
        theirs = await client.post(
            "/applications",
            headers=other_owner.headers,
            json={
                "company_id": their_company.json()["id"],
                "job_code": "REQ-12345",
            },
        )
        assert theirs.status_code == 201, theirs.text

        mine = await self._create(
            client, admin, company["id"], job_title="Engineer", job_code="REQ-12345"
        )
        assert mine.status_code == 201, mine.text
        assert mine.json()["job_code_match_count"] == 0

    async def test_it_is_audited(self, client, admin, company):
        created = await self._create(
            client, admin, company["id"], job_title="Engineer", job_code="REQ-12345"
        )
        audit = await client.get(
            "/audit",
            headers=admin.headers,
            params={"table": "applications", "row_id": str(created.json()["id"])},
        )
        assert audit.json()["data"][0]["new_data"]["job_code"] == "REQ-12345"
