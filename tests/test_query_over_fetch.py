"""Persistence-level coverage for `SQAlchemyBase.light` / `heavy_columns`.

See `QUERY_PERFORMANCE_PLAN.md` Phase 1. `tests/test_query_budgets.py`
covers the response-shape half of Defect B (a heavy column never reaches the
wire); this file covers the persistence half: a `light()` query defers the
column with `raiseload=True`, and the non-`light()` siblings that a real
consumer still needs keep loading it in full.
"""

import base64

import pytest
from sqlalchemy import select
from sqlalchemy.exc import InvalidRequestError

from persistence.application import Application
from persistence.application_attachment import ApplicationAttachment
from persistence.company import Company
from persistence.document import Document
from persistence.user import User
from services.database.database_service import DatabaseService

PDF_BASE64 = base64.b64encode(b"%PDF-1.4\n%fake pdf content for testing\n").decode()


async def test_application_light_defers_heavy_columns(client, admin, company):
    response = await client.post(
        "/applications",
        headers=admin.headers,
        json={
            "company_id": company["id"],
            "job_title": "Engineer",
            "job_description": "a very long posting",
            "initial_prompt_text": "prompt",
            "modification_note": "note",
        },
    )
    assert response.status_code == 201, response.text

    async with DatabaseService.session() as db:
        deferred = (
            await Application.list_for_company(db, admin.user_id, company["id"])
        )[0]
        with pytest.raises(InvalidRequestError):
            _ = deferred.job_description

        loaded = await Application.get(db, admin.user_id, deferred.id)
        assert loaded is not None
        assert loaded.job_description == "a very long posting"


async def test_document_light_defers_data(client, admin, stored_resume):
    async with DatabaseService.session() as db:
        deferred = (await Document.list_latest(db, admin.user_id))[0]
        with pytest.raises(InvalidRequestError):
            _ = deferred.data

        loaded = await Document.get_latest(db, admin.user_id, deferred.name)
        assert loaded is not None
        assert loaded.data


async def test_application_attachment_light_defers_content(client, admin, application):
    response = await client.post(
        f"/applications/{application['id']}/attachments",
        headers=admin.headers,
        json={
            "kind": "resume",
            "filename": "resume.pdf",
            "content_type": "application/pdf",
            "content_base64": PDF_BASE64,
        },
    )
    assert response.status_code == 201, response.text

    async with DatabaseService.session() as db:
        deferred = (
            await ApplicationAttachment.list_metadata_for_application(
                db, admin.user_id, application["id"]
            )
        )[0]
        with pytest.raises(InvalidRequestError):
            _ = deferred.content_base64

        loaded = await ApplicationAttachment.get_with_content(
            db, admin.user_id, deferred.id
        )
        assert loaded is not None
        assert loaded.content_base64 == PDF_BASE64


def test_light_is_a_noop_for_a_model_with_no_heavy_columns():
    """`Company` never overrides `heavy_columns`, so `light()` must hand back
    the statement it was given rather than add empty defer options."""
    assert Company.heavy_columns() == ()
    stmt = select(Company)
    assert Company.light(stmt) is stmt


async def test_user_any_with_role_defers_password(admin):
    """`any_with_role` reads only `roles`, in one whole-table scan - see
    persistence/user.py. Confirms the scan still answers correctly with
    `password` deferred, and that the deferral is real."""
    async with DatabaseService.session() as db:
        assert await User.any_with_role(db, "admin") is True
        assert await User.any_with_role(db, "no-such-role") is False


async def test_update_application_after_create_does_not_touch_deferred_load(
    client, admin, company
):
    """`ApplicationService.update_application` loads its row with
    `Application.get` (not `light`) and calls `is_modified` on it - neither
    step should trip the `raiseload` guard the heavy columns now carry."""
    response = await client.post(
        "/applications",
        headers=admin.headers,
        json={
            "company_id": company["id"],
            "job_title": "Engineer",
            "job_description": "original posting",
        },
    )
    assert response.status_code == 201, response.text
    application_id = response.json()["id"]

    response = await client.patch(
        f"/applications/{application_id}",
        headers=admin.headers,
        json={"job_description": "revised posting"},
    )
    assert response.status_code == 200, response.text

    response = await client.get(
        f"/applications/{application_id}", headers=admin.headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["job_description"] == "revised posting"
