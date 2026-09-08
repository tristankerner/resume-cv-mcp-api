from datetime import datetime

from sqlalchemy import ForeignKey, Index, Text, UniqueConstraint, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .base import Clock, SQAlchemyBase


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
    ) -> tuple[list[Company], int]:
        conditions = [Company.user_id == user_id]
        if query:
            conditions.append(func.lower(Company.name).like(f"%{query.lower()}%"))

        total = (
            await db.execute(
                select(func.count()).select_from(Company).where(*conditions)
            )
        ).scalar_one()

        order = {
            "name": Company.name.asc(),
            "-name": Company.name.desc(),
            "created_at": Company.created_at.asc(),
            "-created_at": Company.created_at.desc(),
        }.get(sort, Company.name.asc())

        rows = list(
            (
                await db.execute(
                    select(Company)
                    .where(*conditions)
                    .order_by(order)
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return rows, total

    @staticmethod
    async def names_for(
        db: AsyncSession, user_id: int, company_ids: list[int]
    ) -> dict[int, str]:
        """Name per company id, in one query. Filtered on the owner like every
        other read here, so an id belonging to someone else simply does not
        come back."""
        if not company_ids:
            return {}
        rows = (
            await db.execute(
                select(Company.id, Company.name).where(
                    Company.user_id == user_id, Company.id.in_(company_ids)
                )
            )
        ).all()
        return {row[0]: row[1] for row in rows}

    @staticmethod
    async def counts_for(
        db: AsyncSession, user_id: int, company_ids: list[int]
    ) -> dict[int, tuple[int, int]]:
        """`(application_count, contact_count)` per company id, via two grouped
        aggregate queries rather than a count per row."""
        if not company_ids:
            return {}

        from .application import Application
        from .contact import Contact

        application_rows = (
            await db.execute(
                select(Application.company_id, func.count())
                .where(
                    Application.user_id == user_id,
                    Application.company_id.in_(company_ids),
                )
                .group_by(Application.company_id)
            )
        ).all()
        contact_rows = (
            await db.execute(
                select(Contact.company_id, func.count())
                .where(Contact.user_id == user_id, Contact.company_id.in_(company_ids))
                .group_by(Contact.company_id)
            )
        ).all()
        applications = {row[0]: row[1] for row in application_rows}
        contacts = {row[0]: row[1] for row in contact_rows}
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
