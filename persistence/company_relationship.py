from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .base import Clock, SQAlchemyBase


class CompanyRelationship(SQAlchemyBase):
    """A directed edge between two of the caller's own companies.

    `user_id` duplicates what a join to either company would already say —
    kept anyway because every audit trigger and every permission filter in
    this feature reads `user_id` straight off the row it is looking at.
    """

    __tablename__ = "company_relationships"
    __table_args__ = (
        CheckConstraint(
            "from_company_id <> to_company_id",
            name="ck_company_relationships_not_self",
        ),
        UniqueConstraint(
            "user_id",
            "from_company_id",
            "to_company_id",
            "type",
            name="uq_company_relationships_edge",
        ),
        Index("ix_company_relationships_user_from", "user_id", "from_company_id"),
        Index("ix_company_relationships_user_to", "user_id", "to_company_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    from_company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    to_company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)

    def __repr__(self) -> str:
        return (
            f"CompanyRelationship(id={self.id!r}, "
            f"from_company_id={self.from_company_id!r}, "
            f"to_company_id={self.to_company_id!r}, type={self.type!r})"
        )

    @staticmethod
    async def get(
        db: AsyncSession, user_id: int, relationship_id: int
    ) -> CompanyRelationship | None:
        return (
            (
                await db.execute(
                    select(CompanyRelationship).where(
                        CompanyRelationship.id == relationship_id,
                        CompanyRelationship.user_id == user_id,
                    )
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def list_for_company(
        db: AsyncSession, user_id: int, company_id: int
    ) -> list[CompanyRelationship]:
        """Edges in both directions - a relationship is meaningful from either
        endpoint's page."""
        return list(
            (
                await db.execute(
                    select(CompanyRelationship).where(
                        CompanyRelationship.user_id == user_id,
                        or_(
                            CompanyRelationship.from_company_id == company_id,
                            CompanyRelationship.to_company_id == company_id,
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
