"""Coverage for `Page.fetch`'s round-trip count and its empty-page trap.

See `QUERY_PERFORMANCE_PLAN.md` §3.2 and Phase 3. `count(*) OVER ()` returns
no rows when the page itself is empty, so an offset past the end of a
non-empty result set would otherwise read back `total=0` - wrong, and
exactly the case that breaks pagination in a client that trusts `total` to
decide whether to keep paging. `Company.search` stands in for all four
`search` methods `Page.fetch` now backs; the fetch/window logic is shared,
so one persistence-level suite covers it for all of them.
"""

import secrets

from persistence.company import Company
from services.database.database_service import DatabaseService
from tests.support.query_recorder import QueryRecorder


async def _make_company(client, admin) -> None:
    response = await client.post(
        "/companies",
        headers=admin.headers,
        json={"name": f"Co {secrets.token_hex(4)}"},
    )
    assert response.status_code == 201, response.text


async def test_page_within_range_reports_rows_and_total(client, admin):
    for _ in range(5):
        await _make_company(client, admin)

    async with DatabaseService.session() as db:
        rows, total = await Company.search(db, admin.user_id, None, 2, 1, "name")

    assert len(rows) == 2
    assert total == 5


async def test_offset_past_end_of_nonempty_set_reports_correct_total(client, admin):
    """The trap: without the `total_stmt` fallback, this would read back
    `total=0` because the windowed query returns no rows for this page."""
    for _ in range(3):
        await _make_company(client, admin)

    async with DatabaseService.session() as db:
        rows, total = await Company.search(db, admin.user_id, None, 10, 100, "name")

    assert rows == []
    assert total == 3


async def test_empty_table_at_offset_zero_skips_the_fallback_query(admin):
    """`offset == 0` with no rows is a real zero, not an overrun - answered
    from the windowed query alone, with no second round trip."""
    async with DatabaseService.session() as db:
        with QueryRecorder() as recorder:
            rows, total = await Company.search(db, admin.user_id, None, 10, 0, "name")

    assert rows == []
    assert total == 0
    assert recorder.count() == 1


async def test_offset_past_end_of_empty_table_pays_the_fallback_query(admin):
    """Same zero result as the case above, reached the slow way: the
    windowed query alone cannot tell "nothing exists" from "overran a page
    that has rows", so the fallback runs and confirms the total is 0."""
    async with DatabaseService.session() as db:
        with QueryRecorder() as recorder:
            rows, total = await Company.search(db, admin.user_id, None, 10, 50, "name")

    assert rows == []
    assert total == 0
    assert recorder.count() == 2
