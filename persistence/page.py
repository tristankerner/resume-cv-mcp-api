from typing import Any

from sqlalchemy import Select, func
from sqlalchemy.ext.asyncio import AsyncSession


class Page:
    """A page of rows and the unpaged total, in one round trip.

    `count(*) OVER ()` rides along as an extra column instead of a second
    SELECT, which is where the four `search` helpers each lost a round trip.

    Returns bare `tuple`s of whatever `stmt` selects, with the window column
    sliced off - not the single unwrapped entity a `select(Model)` caller
    might expect. `RelatedCount` columns ride in the same row as the entity
    they describe, so the caller is the only one who knows whether a row is
    "the entity" or "the entity plus its correlated counts"; unwrapping here
    would have to guess.
    """

    @staticmethod
    async def fetch(
        db: AsyncSession,
        stmt: Select[tuple[Any, ...]],
        *,
        limit: int,
        offset: int,
        total_stmt: Select[tuple[int]],
    ) -> tuple[list[tuple[Any, ...]], int]:
        """`total_stmt` is a fallback, not the common path: a window
        function returns no rows when the page itself is empty, so the total
        would otherwise read as zero even when earlier pages exist. That
        fallback only fires for an empty page past the start - `offset == 0`
        with no rows means the total genuinely is zero, and needs no second
        query to say so.
        """
        windowed = (
            stmt.add_columns(func.count().over().label("_page_total"))
            .limit(limit)
            .offset(offset)
        )
        rows = (await db.execute(windowed)).all()
        if rows:
            return [tuple(row[:-1]) for row in rows], rows[0][-1]
        if offset > 0:
            total = (await db.execute(total_stmt)).scalar_one()
            return [], total
        return [], 0
