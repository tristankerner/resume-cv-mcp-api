from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .base import SQAlchemyBase


class RoleScope(SQAlchemyBase):
    """One (role, scope) grant. The pair is the fact, so the composite primary
    key gives the uniqueness constraint for free rather than needing a
    surrogate id nothing else ever references.

    Reference data: nothing in the API writes this table. It is seeded and
    changed by migration, or by hand — see services/auth/scopes.py for the
    cache built on top of it.
    """

    __tablename__ = "role_scopes"
    role: Mapped[str] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(primary_key=True)

    @staticmethod
    async def all_pairs(db: AsyncSession) -> list[tuple[str, str]]:
        result = await db.execute(select(RoleScope.role, RoleScope.scope))
        return [(row.role, row.scope) for row in result]
