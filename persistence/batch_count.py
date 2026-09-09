from collections.abc import Collection
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute


class BatchCount:
    """`{key: count}` for a set of keys, in one owner-scoped grouped query.

    The shape every `counts_for` in this codebase already had, generalized:
    `ApplicationEvent.counts_for`, `ApplicationAttachment.counts_for`,
    `Company.counts_for` (twice, once per child table) and
    `Application.job_code_match_counts` were five near-identical copies of
    this same grouped count.
    """

    @staticmethod
    async def for_keys(
        db: AsyncSession,
        *,
        key_column: InstrumentedAttribute[Any],
        owner_column: InstrumentedAttribute[int],
        owner_id: int,
        keys: Collection[Any],
    ) -> dict[Any, int]:
        if not keys:
            return {}
        rows = (
            await db.execute(
                select(key_column, func.count())
                .where(owner_column == owner_id, key_column.in_(keys))
                .group_by(key_column)
            )
        ).all()
        counts = {row[0]: row[1] for row in rows}
        return {key: counts.get(key, 0) for key in keys}
