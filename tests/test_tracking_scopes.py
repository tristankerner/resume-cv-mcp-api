"""Every tracking route: 401 with no credential, 403 with the wrong scope,
404 for another user's row. See section 12 of the tracking plan - this is
the consolidated sweep; the per-feature test files also cover a subset of
this inline.
"""

import base64

import pytest

PDF_BASE64 = base64.b64encode(b"%PDF-1.4\nfake\n").decode()


@pytest.fixture
async def context(client, admin) -> dict:
    """One of everything, owned by `admin`, created through the API."""
    company = (
        await client.post(
            "/companies", headers=admin.headers, json={"name": "Acme Inc"}
        )
    ).json()
    other_company = (
        await client.post(
            "/companies", headers=admin.headers, json={"name": "Globex Corp"}
        )
    ).json()
    contact = (
        await client.post(
            "/contacts",
            headers=admin.headers,
            json={"company_id": company["id"], "first_name": "Sam"},
        )
    ).json()
    application = (
        await client.post(
            "/applications",
            headers=admin.headers,
            json={"company_id": company["id"], "job_title": "Engineer"},
        )
    ).json()
    event = (
        await client.post(
            f"/applications/{application['id']}/events",
            headers=admin.headers,
            json={"description": "note"},
        )
    ).json()["event"]
    relationship = (
        await client.post(
            f"/companies/{company['id']}/relationships",
            headers=admin.headers,
            json={"to_company_id": other_company["id"], "type": "child_of"},
        )
    ).json()
    stack_item = (
        await client.post(
            f"/companies/{company['id']}/stack",
            headers=admin.headers,
            json={"name": "Go", "type": "programming_language"},
        )
    ).json()
    attachment = (
        await client.post(
            f"/applications/{application['id']}/attachments",
            headers=admin.headers,
            json={
                "kind": "resume",
                "filename": "r.pdf",
                "content_type": "application/pdf",
                "content_base64": PDF_BASE64,
            },
        )
    ).json()
    return {
        "company_id": company["id"],
        "other_company_id": other_company["id"],
        "contact_id": contact["id"],
        "application_id": application["id"],
        "event_id": event["id"],
        "relationship_id": relationship["id"],
        "stack_item_id": stack_item["id"],
        "attachment_id": attachment["id"],
    }


# (method, path template, scope, body-or-None, check_ownership_404)
ROUTES = [
    ("GET", "/companies", "companies:read", None, False),
    ("POST", "/companies", "companies:write", {"name": "New Co"}, False),
    ("GET", "/companies/{company_id}", "companies:read", None, True),
    ("PATCH", "/companies/{company_id}", "companies:write", {"description": "x"}, True),
    ("DELETE", "/companies/{company_id}", "companies:delete", None, True),
    (
        "POST",
        "/companies/{company_id}/relationships",
        "companies:write",
        {"to_company_id": "{other_company_id}", "type": "child_of"},
        True,
    ),
    ("GET", "/companies/{company_id}/stack", "companies:read", None, True),
    (
        "POST",
        "/companies/{company_id}/stack",
        "companies:write",
        {"name": "Rust", "type": "programming_language"},
        True,
    ),
    (
        "PATCH",
        "/company-relationships/{relationship_id}",
        "companies:write",
        {"note": "x"},
        True,
    ),
    (
        "DELETE",
        "/company-relationships/{relationship_id}",
        "companies:delete",
        None,
        True,
    ),
    (
        "PATCH",
        "/company-stack/{stack_item_id}",
        "companies:write",
        {"description": "x"},
        True,
    ),
    ("DELETE", "/company-stack/{stack_item_id}", "companies:delete", None, True),
    ("GET", "/contacts", "contacts:read", None, False),
    ("POST", "/contacts", "contacts:write", {"first_name": "Alex"}, False),
    ("GET", "/contacts/{contact_id}", "contacts:read", None, True),
    ("PATCH", "/contacts/{contact_id}", "contacts:write", {"phone": "555-0100"}, True),
    ("DELETE", "/contacts/{contact_id}", "contacts:delete", None, True),
    ("GET", "/applications", "applications:read", None, False),
    (
        "POST",
        "/applications",
        "applications:write",
        {"company_id": "{company_id}"},
        False,
    ),
    ("GET", "/applications/{application_id}/events", "applications:read", None, True),
    (
        "POST",
        "/applications/{application_id}/events",
        "applications:write",
        {"description": "another note"},
        True,
    ),
    (
        "GET",
        "/applications/{application_id}/attachments",
        "applications:read",
        None,
        True,
    ),
    (
        "POST",
        "/applications/{application_id}/attachments",
        "applications:write",
        {
            "kind": "resume",
            "filename": "second.pdf",
            "content_type": "application/pdf",
            "content_base64": PDF_BASE64,
        },
        True,
    ),
    ("GET", "/applications/{application_id}", "applications:read", None, True),
    (
        "PATCH",
        "/applications/{application_id}",
        "applications:write",
        {"source": "LinkedIn"},
        True,
    ),
    ("DELETE", "/applications/{application_id}", "applications:delete", None, True),
    (
        "PATCH",
        "/application-events/{event_id}",
        "applications:write",
        {"description": "edited"},
        True,
    ),
    ("DELETE", "/application-events/{event_id}", "applications:delete", None, True),
    ("GET", "/attachments/{attachment_id}", "applications:read", None, True),
    ("DELETE", "/attachments/{attachment_id}", "applications:delete", None, True),
    ("GET", "/audit", "audit:read", None, False),
]


def _fill(value, context: dict):
    if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
        return context[value[1:-1]]
    if isinstance(value, dict):
        return {k: _fill(v, context) for k, v in value.items()}
    return value


def _route_id(route: tuple) -> str:
    method, path, scope, _body, _check = route
    return f"{method} {path} [{scope}]"


class TestEveryTrackingRoute:
    @pytest.mark.parametrize("route", ROUTES, ids=_route_id)
    async def test_requires_a_credential(self, client, context, route):
        method, path_template, _scope, body, _check = route
        path = path_template.format(**context)
        filled_body = _fill(body, context)
        response = await client.request(method, path, json=filled_body)
        assert response.status_code == 401

    @pytest.mark.parametrize("route", ROUTES, ids=_route_id)
    async def test_wrong_scope_is_403(self, client, roleless, context, route):
        method, path_template, _scope, body, _check = route
        path = path_template.format(**context)
        filled_body = _fill(body, context)
        response = await client.request(
            method, path, headers=roleless.headers, json=filled_body
        )
        assert response.status_code == 403

    @pytest.mark.parametrize(
        "route", [route for route in ROUTES if route[4]], ids=_route_id
    )
    async def test_another_users_row_is_404_not_403(
        self, client, other_owner, context, route
    ):
        method, path_template, _scope, body, _check = route
        path = path_template.format(**context)
        filled_body = _fill(body, context)
        response = await client.request(
            method, path, headers=other_owner.headers, json=filled_body
        )
        assert response.status_code == 404
