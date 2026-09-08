from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .base import Clock, SQAlchemyBase


class Contact(SQAlchemyBase):
    __tablename__ = "contacts"
    __table_args__ = (
        CheckConstraint(
            "rating IS NULL OR (rating BETWEEN 1 AND 10)",
            name="ck_contacts_rating_range",
        ),
        # The stated primary access path: "Contacts will almost always be
        # queried via Company Id and User Id".
        Index("ix_contacts_user_company", "user_id", "company_id"),
        Index("ix_contacts_user_name", "user_id", "last_name", "first_name"),
        Index("ix_contacts_user_email", "user_id", "email"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    # Nullable: an independent recruiter has no company row yet.
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL")
    )
    first_name: Mapped[str | None]
    last_name: Mapped[str | None]
    normalized_name: Mapped[str | None]
    email: Mapped[str | None]
    phone: Mapped[str | None]
    description: Mapped[str | None] = mapped_column(Text)
    personal_note: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)

    def __repr__(self) -> str:
        return (
            f"Contact(id={self.id!r}, user_id={self.user_id!r}, "
            f"normalized_name={self.normalized_name!r})"
        )

    @staticmethod
    async def get(db: AsyncSession, user_id: int, contact_id: int) -> Contact | None:
        return (
            (
                await db.execute(
                    select(Contact).where(
                        Contact.id == contact_id, Contact.user_id == user_id
                    )
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def detach_from_company(
        db: AsyncSession, user_id: int, company_id: int
    ) -> None:
        """Null `company_id` on every one of this owner's contacts at that
        company, ahead of the company being deleted. Does not commit - the
        caller owns the transaction."""
        await db.execute(
            update(Contact)
            .where(Contact.user_id == user_id, Contact.company_id == company_id)
            .values(company_id=None)
        )

    @staticmethod
    async def search(
        db: AsyncSession,
        user_id: int,
        query: str | None,
        company_id: int | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Contact], int]:
        conditions = [Contact.user_id == user_id]
        if company_id is not None:
            conditions.append(Contact.company_id == company_id)
        if query:
            pattern = f"%{query.lower()}%"
            conditions.append(
                func.lower(
                    func.coalesce(Contact.first_name, "")
                    + " "
                    + func.coalesce(Contact.last_name, "")
                ).like(pattern)
                | func.lower(func.coalesce(Contact.email, "")).like(pattern)
            )

        total = (
            await db.execute(
                select(func.count()).select_from(Contact).where(*conditions)
            )
        ).scalar_one()
        rows = list(
            (
                await db.execute(
                    select(Contact)
                    .where(*conditions)
                    .order_by(Contact.last_name, Contact.first_name)
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return rows, total

    @staticmethod
    async def get_by_email(
        db: AsyncSession, user_id: int, email: str
    ) -> Contact | None:
        return (
            (
                await db.execute(
                    select(Contact).where(
                        Contact.user_id == user_id, Contact.email == email
                    )
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def candidates_for_dedup(
        db: AsyncSession, user_id: int, company_id: int | None
    ) -> list[tuple[int, str, str]]:
        """`(id, display_name, normalized_name)` for the contacts a new one at
        `company_id` should be compared against - contacts with no normalized
        name (both name parts empty) cannot match on name and are excluded."""
        rows = (
            await db.execute(
                select(
                    Contact.id,
                    func.coalesce(Contact.first_name, "")
                    + " "
                    + func.coalesce(Contact.last_name, ""),
                    Contact.normalized_name,
                ).where(
                    Contact.user_id == user_id,
                    Contact.company_id == company_id,
                    Contact.normalized_name.is_not(None),
                )
            )
        ).all()
        return [(row[0], row[1].strip(), row[2]) for row in rows]
