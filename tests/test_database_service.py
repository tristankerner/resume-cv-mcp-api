"""Pool configuration coverage for `DatabaseService`.

See `QUERY_PERFORMANCE_PLAN.md` Phase 5. `pool_pre_ping` spent a round trip
on every checkout to guard against a Neon-suspended connection; `pool_recycle`
alone gets the same protection at 240s (under Neon's five-minute suspend)
without paying for a ping on a connection that was never at risk. Both
settings are no-ops against SQLite at runtime, so this asserts the engine's
configuration directly rather than observing behavior.
"""

from services.database.database_service import DatabaseService


def test_pool_recycles_before_neons_suspend_window():
    pool = DatabaseService.engine().pool
    assert pool._recycle == 240


def test_pool_pre_ping_is_not_enabled():
    """A stale connection is caught by `pool_recycle` instead - see
    `DatabaseService._ensure_built`. Re-enabling `pool_pre_ping` on top of
    that would pay for a ping every checkout already protected by recycle."""
    pool = DatabaseService.engine().pool
    assert pool._pre_ping is False
