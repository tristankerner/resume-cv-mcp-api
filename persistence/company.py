from datetime import datetime
from typing import NamedTuple

from sqlalchemy import ForeignKey, Index, Text, UniqueConstraint, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .base import Clock, SQAlchemyBase
from .batch_count import BatchCount
from .lookup import Lookup
from .page import Page
from .related_count import RelatedCount


class Company(SQAlchemyBase):
    __tablename__ = "companies"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "normalized_name", name="uq_companies_user_normalized_name"
        ),
        Index("ix_companies_user_name", "user_id", "name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(nullable=False)
    normalized_name: Mapped[str] = mapped_column(nullable=False)
    website: Mapped[str | None]
    description: Mapped[str | None] = mapped_column(Text)
    personal_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)

    def __repr__(self) -> str:
        return f"Company(id={self.id!r}, user_id={self.user_id!r}, name={self.name!r})"

    @staticmethod
    async def get(db: AsyncSession, user_id: int, company_id: int) -> Company | None:
        return (
            (
                await db.execute(
                    select(Company).where(
                        Company.id == company_id, Company.user_id == user_id
                    )
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def get_by_normalized_name(
        db: AsyncSession, user_id: int, normalized_name: str
    ) -> Company | None:
        return (
            (
                await db.execute(
                    select(Company).where(
                        Company.user_id == user_id,
                        Company.normalized_name == normalized_name,
                    )
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def search(
        db: AsyncSession,
        user_id: int,
        query: str | None,
        limit: int,
        offset: int,
        sort: str = "name",
    ) -> tuple[list[CompanySearchRow], int]:
        from .application import Application
        from .contact import Contact

        conditions = [Company.user_id == user_id]
        if query:
            conditions.append(func.lower(Company.name).like(f"%{query.lower()}%"))

        order = {
            "name": Company.name.asc(),
            "-name": Company.name.desc(),
            "created_at": Company.created_at.asc(),
            "-created_at": Company.created_at.desc(),
        }.get(sort, Company.name.asc())

        row_stmt = (
            select(
                Company,
                RelatedCount.column(
                    child_owner=Application.user_id,
                    child_parent=Application.company_id,
                    parent_key=Company.id,
                    owner_id=user_id,
                    label="application_count",
                ),
                RelatedCount.column(
                    child_owner=Contact.user_id,
                    child_parent=Contact.company_id,
                    parent_key=Company.id,
                    owner_id=user_id,
                    label="contact_count",
                ),
            )
            .where(*conditions)
            .order_by(order)
        )
        total_stmt = select(func.count()).select_from(Company).where(*conditions)
        rows, total = await Page.fetch(
            db, row_stmt, limit=limit, offset=offset, total_stmt=total_stmt
        )
        return [CompanySearchRow(*row) for row in rows], total

    @staticmethod
    async def names_for(
        db: AsyncSession, user_id: int, company_ids: list[int]
    ) -> dict[int, str]:
        """Name per company id, in one query. Filtered on the owner like every
        other read here, so an id belonging to someone else simply does not
        come back."""
        rows = await Lookup.map(
            db,
            key_column=Company.id,
            value_columns=(Company.name,),
            owner_column=Company.user_id,
            owner_id=user_id,
            keys=company_ids,
        )
        return {company_id: row.name for company_id, row in rows.items()}

    @staticmethod
    async def counts_for(
        db: AsyncSession, user_id: int, company_ids: list[int]
    ) -> dict[int, tuple[int, int]]:
        """`(application_count, contact_count)` per company id, via two grouped
        aggregate queries rather than a count per row.

        Kept as its own batched pair alongside `Company.search`'s correlated
        columns rather than replaced by them: `CompanyService.get_company`
        wants the counts for exactly one row it already has, where a
        correlated subquery would cost the same round trip for no benefit.
        """
        from .application import Application
        from .contact import Contact

        applications = await BatchCount.for_keys(
            db,
            key_column=Application.company_id,
            owner_column=Application.user_id,
            owner_id=user_id,
            keys=company_ids,
        )
        contacts = await BatchCount.for_keys(
            db,
            key_column=Contact.company_id,
            owner_column=Contact.user_id,
            owner_id=user_id,
            keys=company_ids,
        )
        return {
            company_id: (applications.get(company_id, 0), contacts.get(company_id, 0))
            for company_id in company_ids
        }

    @staticmethod
    async def all_normalized_names(
        db: AsyncSession, user_id: int
    ) -> list[tuple[int, str, str]]:
        rows = (
            await db.execute(
                select(Company.id, Company.name, Company.normalized_name).where(
                    Company.user_id == user_id
                )
            )
        ).all()
        return [(row.id, row.name, row.normalized_name) for row in rows]


class CompanySearchRow(NamedTuple):
    """One row of `Company.search`'s result: the entity plus the two
    `RelatedCount` columns a summary needs, so the list endpoint no longer
    follows up with `Company.counts_for`. See `QUERY_PERFORMANCE_PLAN.md`
    Phase 4."""

    company: Company
    application_count: int
    contact_count: int
