from datetime import datetime

from sqlalchemy import ForeignKey, Index, Text, UniqueConstraint, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .base import Clock, SQAlchemyBase


class CompanyStackItem(SQAlchemyBase):
    __tablename__ = "company_stack_items"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "company_id",
            "normalized_name",
            name="uq_company_stack_items_company_name",
        ),
        Index("ix_company_stack_items_user_company", "user_id", "company_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(nullable=False)
    normalized_name: Mapped[str] = mapped_column(nullable=False)
    type: Mapped[str] = mapped_column(nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)

    def __repr__(self) -> str:
        return (
            f"CompanyStackItem(id={self.id!r}, company_id={self.company_id!r}, "
            f"name={self.name!r})"
        )

    @staticmethod
    async def get(
        db: AsyncSession, user_id: int, item_id: int
    ) -> CompanyStackItem | None:
        return (
            (
                await db.execute(
                    select(CompanyStackItem).where(
                        CompanyStackItem.id == item_id,
                        CompanyStackItem.user_id == user_id,
                    )
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def list_for_company(
        db: AsyncSession, user_id: int, company_id: int
    ) -> list[CompanyStackItem]:
        return list(
            (
                await db.execute(
                    select(CompanyStackItem)
                    .where(
                        CompanyStackItem.user_id == user_id,
                        CompanyStackItem.company_id == company_id,
                    )
                    .order_by(CompanyStackItem.name)
                )
            )
            .scalars()
            .all()
        )

    @staticmethod
    async def get_by_normalized_name(
        db: AsyncSession, user_id: int, company_id: int, normalized_name: str
    ) -> CompanyStackItem | None:
        return (
            (
                await db.execute(
                    select(CompanyStackItem).where(
                        CompanyStackItem.user_id == user_id,
                        CompanyStackItem.company_id == company_id,
                        CompanyStackItem.normalized_name == normalized_name,
                    )
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def all_normalized_for_company(
        db: AsyncSession, user_id: int, company_id: int
    ) -> list[tuple[int, str, str]]:
        rows = (
            await db.execute(
                select(
                    CompanyStackItem.id,
                    CompanyStackItem.name,
                    CompanyStackItem.normalized_name,
                ).where(
                    CompanyStackItem.user_id == user_id,
                    CompanyStackItem.company_id == company_id,
                )
            )
        ).all()
        return [(row.id, row.name, row.normalized_name) for row in rows]
