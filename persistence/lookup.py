from collections.abc import Collection, Sequence
from typing import Any

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute


class Lookup:
    """`{key: row}` for a set of keys, in one owner-scoped query.

    The shape `Company.names_for` already had, generalized: every list view
    that decorates its rows with a name held on another table wants exactly
    this, and writing it per call site is what produced the N+1s.
    """

    @staticmethod
    async def map(
        db: AsyncSession,
        *,
        key_column: InstrumentedAttribute[Any],
        value_columns: Sequence[InstrumentedAttribute[Any]],
        owner_column: InstrumentedAttribute[int],
        owner_id: int,
        keys: Collection[Any],
    ) -> dict[Any, Row[Any]]:
        """`value_columns` come back as a `Row` rather than a formatted
        string: some callers need two columns (`Contact.first_name` +
        `last_name`) and some need one (`Company.name`). Formatting stays
        with the caller.

        `owner_column`/`owner_id` are required rather than optional: every
        read in this codebase filters on the owner, and a lookup that forgot
        it would be a cross-tenant leak.
        """
        if not keys:
            return {}
        rows = (
            await db.execute(
                select(key_column, *value_columns).where(
                    owner_column == owner_id, key_column.in_(keys)
                )
            )
        ).all()
        return {row[0]: row for row in rows}
