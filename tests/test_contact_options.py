"""GET /applications/{id}/contact-options — the company-relationship graph
walk and the contacts it surfaces. See FEATURE_EXPANSION_PLAN.md section 4."""

import secrets

from persistence.company import Company
from persistence.company_relationship import CompanyRelationship
from persistence.contact import Contact
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService


async def _company(client, admin, name: str) -> int:
    """`name` gets a random suffix - the create-path dedup heuristic is fuzzy
    enough that "Cycle A" and "Cycle B" collide otherwise, which is correct
    product behaviour and not something these tests want to fight."""
    response = await client.post(
        "/companies",
        headers=admin.headers,
        json={"name": f"{name} {secrets.token_hex(4)}"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _link(
    client, admin, from_id: int, to_id: int, type_: str = "partner_of"
) -> None:
    response = await client.post(
        f"/companies/{from_id}/relationships",
        headers=admin.headers,
        json={"to_company_id": to_id, "type": type_},
    )
    assert response.status_code == 201, response.text


async def _application(client, admin, company_id: int) -> int:
    response = await client.post(
        "/applications", headers=admin.headers, json={"company_id": company_id}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _contact(client, admin, company_id: int, first_name: str) -> int:
    response = await client.post(
        "/contacts",
        headers=admin.headers,
        json={"company_id": company_id, "first_name": first_name},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _api_key(client, actor, *scopes: Scopes) -> dict[str, str]:
    created = await client.post(
        "/api-keys",
        headers=actor.headers,
        json={"name": "narrowed", "scopes": [scope.value for scope in scopes]},
    )
    assert created.status_code == 200, created.text
    return {"Authorization": f"Bearer {created.json()['key']}"}


class TestDepth:
    async def test_depths_zero_through_three_are_returned_depth_four_is_not(
        self, client, admin
    ):
        root = await _company(client, admin, "Root Co")
        a = await _company(client, admin, "Hop A Co")
        b = await _company(client, admin, "Hop B Co")
        c = await _company(client, admin, "Hop C Co")
        d = await _company(client, admin, "Hop D Co")
        await _link(client, admin, root, a)
        await _link(client, admin, a, b)
        await _link(client, admin, b, c)
        await _link(client, admin, c, d)

        application_id = await _application(client, admin, root)
        for company_id, name in (
            (root, "Root"),
            (a, "A"),
            (b, "B"),
            (c, "C"),
            (d, "D"),
        ):
            await _contact(client, admin, company_id, name)

        response = await client.get(
            f"/applications/{application_id}/contact-options", headers=admin.headers
        )
        assert response.status_code == 200, response.text
        by_name = {row["first_name"]: row for row in response.json()["data"]}
        assert by_name["Root"]["depth"] == 0
        assert by_name["Root"]["via"] is None
        assert by_name["A"]["depth"] == 1
        assert by_name["B"]["depth"] == 2
        assert by_name["C"]["depth"] == 3
        assert "D" not in by_name


class TestBothDirections:
    async def test_reachable_regardless_of_edge_direction(self, client, admin):
        root = await _company(client, admin, "Root Co 2")
        upstream = await _company(client, admin, "Upstream Co")
        # The edge points *at* root, not from it.
        await _link(client, admin, upstream, root, type_="vendor_of")

        application_id = await _application(client, admin, root)
        await _contact(client, admin, upstream, "Upstream Person")

        response = await client.get(
            f"/applications/{application_id}/contact-options", headers=admin.headers
        )
        assert response.status_code == 200, response.text
        row = next(
            r for r in response.json()["data"] if r["first_name"] == "Upstream Person"
        )
        assert row["depth"] == 1


class TestCycle:
    async def test_a_cycle_terminates_and_each_company_is_seen_once(
        self, client, admin
    ):
        a = await _company(client, admin, "Cycle A")
        b = await _company(client, admin, "Cycle B")
        c = await _company(client, admin, "Cycle C")
        await _link(client, admin, a, b)
        await _link(client, admin, b, c)
        await _link(client, admin, c, a)

        application_id = await _application(client, admin, a)
        await _contact(client, admin, b, "B Person")
        await _contact(client, admin, c, "C Person")

        response = await client.get(
            f"/applications/{application_id}/contact-options", headers=admin.headers
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        names = [row["first_name"] for row in data]
        assert names.count("B Person") == 1
        assert names.count("C Person") == 1
        by_name = {row["first_name"]: row for row in data}
        # Both are one hop from `a` in an undirected sense: A->B directly,
        # and C->A means A can reach C directly too. A cycle must not
        # inflate either past its true minimum depth.
        assert by_name["B Person"]["depth"] == 1
        assert by_name["C Person"]["depth"] == 1


class TestCap:
    async def test_the_200_company_cap_keeps_every_depth_0_and_depth_1_company(
        self, client, admin
    ):
        """depth 0 (1 company) + depth 1 (6 companies) = 7, well under the
        200 cap, so every one of them survives. depth 2 gets 196 companies,
        which pushes the total past 200 - the cap truncates *there*, by
        company id, and never touches depth 0 or depth 1.

        Seeded directly against the database rather than through the API:
        200-odd companies, edges and contacts one HTTP round trip at a time
        would make this the slowest test in the suite for no benefit - what
        it exercises is the query's LIMIT and ORDER BY, not the write path.
        """
        async with DatabaseService.session() as db:
            owner = admin.user_id
            root = Company(user_id=owner, name="Cap Root", normalized_name="cap root")
            db.add(root)
            await db.flush()

            depth1 = [
                Company(
                    user_id=owner, name=f"Cap D1 {i}", normalized_name=f"cap d1 {i}"
                )
                for i in range(6)
            ]
            db.add_all(depth1)
            await db.flush()
            for company in depth1:
                db.add(
                    CompanyRelationship(
                        user_id=owner,
                        from_company_id=root.id,
                        to_company_id=company.id,
                        type="partner_of",
                    )
                )
            await db.flush()

            depth2 = [
                Company(
                    user_id=owner, name=f"Cap D2 {i}", normalized_name=f"cap d2 {i}"
                )
                for i in range(196)
            ]
            db.add_all(depth2)
            await db.flush()
            for parent, company in zip(depth1 * 33, depth2, strict=False):
                db.add(
                    CompanyRelationship(
                        user_id=owner,
                        from_company_id=parent.id,
                        to_company_id=company.id,
                        type="partner_of",
                    )
                )
            await db.flush()

            for company in (root, *depth1, *depth2):
                db.add(Contact(user_id=owner, company_id=company.id, first_name="P"))
            await db.commit()
            root_id = root.id
            depth1_ids = {c.id for c in depth1}

        application_id = await _application(client, admin, root_id)

        response = await client.get(
            f"/applications/{application_id}/contact-options",
            headers=admin.headers,
            params={"limit": 500},
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert len(data) == 200
        returned_company_ids = {row["company_id"] for row in data}
        assert root_id in returned_company_ids
        assert depth1_ids <= returned_company_ids


class TestOwnershipIsolation:
    async def test_another_users_edges_and_contacts_are_never_reachable(
        self, client, admin, other_owner
    ):
        root = await _company(client, admin, "Iso Root")
        their_root = await _company(client, other_owner, "Their Root")
        their_neighbor = await _company(client, other_owner, "Their Neighbor")
        await _link(client, other_owner, their_root, their_neighbor)
        await _contact(client, other_owner, their_neighbor, "Their Person")

        application_id = await _application(client, admin, root)
        response = await client.get(
            f"/applications/{application_id}/contact-options", headers=admin.headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"] == []

        # Nor can admin borrow the other owner's company as a root by
        # guessing their application id.
        their_application_id = await _application(client, other_owner, their_root)
        cross = await client.get(
            f"/applications/{their_application_id}/contact-options",
            headers=admin.headers,
        )
        assert cross.status_code == 404


class TestQueryFilter:
    async def test_query_filters_within_the_graph(self, client, admin):
        root = await _company(client, admin, "Query Root")
        await _contact(client, admin, root, "Dana")
        await _contact(client, admin, root, "Sam")

        application_id = await _application(client, admin, root)
        response = await client.get(
            f"/applications/{application_id}/contact-options",
            headers=admin.headers,
            params={"query": "dana"},
        )
        assert response.status_code == 200, response.text
        names = [row["first_name"] for row in response.json()["data"]]
        assert names == ["Dana"]


class TestLimitTruncatesByDistance:
    """`limit` has to drop the most distant contacts, not an arbitrary
    slice. The contacts query orders by the caller's company preference
    list - which arrives ordered by graph depth - rather than by
    `company_id`, so a root company that happens to hold a high id does not
    lose its own people to a three-hop stranger with a lower one.
    """

    async def test_the_direct_company_survives_truncation(self, client, admin):
        # Created first, so it holds the *lowest* company id of the three,
        # and a truncation ordered by company_id would keep its contacts by
        # luck rather than by design.
        far = await _company(client, admin, "Far Co")
        middle = await _company(client, admin, "Middle Co")
        root = await _company(client, admin, "Root Co")

        await _link(client, admin, root, middle)
        await _link(client, admin, middle, far)

        for index in range(3):
            await _contact(client, admin, far, f"Far{index}")
        await _contact(client, admin, root, "Direct")

        application_id = await _application(client, admin, root)
        response = await client.get(
            f"/applications/{application_id}/contact-options",
            headers=admin.headers,
            params={"limit": 1},
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert len(data) == 1
        assert data[0]["first_name"] == "Direct"
        assert data[0]["depth"] == 0


class TestScopes:
    async def test_missing_contacts_read_is_403_even_with_applications_read(
        self, client, admin
    ):
        company_id = await _company(client, admin, "Scope Co")
        application_id = await _application(client, admin, company_id)
        headers = await _api_key(client, admin, Scopes.APPLICATIONS_READ)

        response = await client.get(
            f"/applications/{application_id}/contact-options", headers=headers
        )
        assert response.status_code == 403

    async def test_missing_applications_read_is_403_even_with_contacts_read(
        self, client, admin
    ):
        company_id = await _company(client, admin, "Scope Co 2")
        application_id = await _application(client, admin, company_id)
        headers = await _api_key(client, admin, Scopes.CONTACTS_READ)

        response = await client.get(
            f"/applications/{application_id}/contact-options", headers=headers
        )
        assert response.status_code == 403

    async def test_both_scopes_present_succeeds(self, client, admin):
        company_id = await _company(client, admin, "Scope Co 3")
        application_id = await _application(client, admin, company_id)
        headers = await _api_key(
            client, admin, Scopes.APPLICATIONS_READ, Scopes.CONTACTS_READ
        )

        response = await client.get(
            f"/applications/{application_id}/contact-options", headers=headers
        )
        assert response.status_code == 200, response.text

    async def test_requires_a_credential(self, client, admin):
        company_id = await _company(client, admin, "Scope Co 4")
        application_id = await _application(client, admin, company_id)
        response = await client.get(f"/applications/{application_id}/contact-options")
        assert response.status_code == 401
