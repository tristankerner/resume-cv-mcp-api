from typing import Any

from sqlalchemy import Label, func, select
from sqlalchemy.orm import InstrumentedAttribute


class RelatedCount:
    """A correlated `(SELECT count(*) ...)` that rides along with the page,
    so a per-row count costs no round trip of its own.

    Cheaper than the grouped follow-up query it replaces only because the
    child tables are already indexed on `(user_id, parent_id)` - see the
    index audit in `QUERY_PERFORMANCE_PLAN.md` §3.4. Adding one of these
    against an unindexed child is a sequential scan per row; check the index
    first.
    """

    @staticmethod
    def column(
        *,
        child_owner: InstrumentedAttribute[int],
        child_parent: InstrumentedAttribute[Any],
        parent_key: InstrumentedAttribute[Any],
        owner_id: int,
        label: str,
    ) -> Label[int]:
        """`scalar_subquery()` correlates to the enclosing select
        automatically - `parent_key` is not selected FROM within this
        subquery, so SQLAlchemy recognizes it as coming from the outer
        query and drops it from this subquery's own FROM clause."""
        return (
            select(func.count())
            .where(child_parent == parent_key, child_owner == owner_id)
            .scalar_subquery()
            .label(label)
        )
