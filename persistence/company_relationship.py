from datetime import datetime
from typing import ClassVar, NamedTuple

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    case,
    cast,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .base import Clock, SQAlchemyBase


class RelatedCompany(NamedTuple):
    """One row of `CompanyRelationship.related_company_ids` - a company
    reachable from the root, at its minimum depth, with the chain of company
    ids that reaches it (empty at depth 0)."""

    company_id: int
    depth: int
    path: str


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

    # See FEATURE_EXPANSION_PLAN.md section 1: settled with the repository
    # owner, not tunable.
    MAX_RELATIONSHIP_DEPTH: ClassVar[int] = 3
    MAX_RELATED_COMPANIES: ClassVar[int] = 200

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

    @staticmethod
    async def related_company_ids(
        db: AsyncSession, user_id: int, root_company_id: int
    ) -> list[RelatedCompany]:
        """Every company reachable from `root_company_id` through this
        owner's `company_relationships`, edges followed in both directions,
        up to `MAX_RELATIONSHIP_DEPTH` hops, capped at
        `MAX_RELATED_COMPANIES` distinct companies ordered
        `(depth, company_id)`. `root_company_id` itself comes back at depth 0
        with an empty path. Does not itself check that `root_company_id` is
        owned by `user_id` - the caller resolves and owns that check, since
        this only ever runs against a company id the caller already read.

        One recursive CTE, not a Python-side BFS - see
        FEATURE_EXPANSION_PLAN.md section 4.3. Cycles terminate on the depth
        bound rather than a visited set: a per-company rank over depth (the
        `ranked` subquery below) collapses the repeats a cycle produces,
        keeping one path per company at its minimum depth. `user_id` is
        filtered inside the recursive term, so another owner's edges are
        never walked to begin with.
        """
        anchor = select(
            literal(root_company_id).label("company_id"),
            literal(0).label("depth"),
            literal("").label("path"),
        ).cte(name="reachable", recursive=True)

        r = CompanyRelationship
        step = (
            select(
                case(
                    (r.from_company_id == anchor.c.company_id, r.to_company_id),
                    else_=r.from_company_id,
                ).label("company_id"),
                (anchor.c.depth + 1).label("depth"),
                anchor.c.path.concat(">")
                .concat(cast(anchor.c.company_id, String))
                .label("path"),
            )
            .select_from(r)
            .join(anchor, anchor.c.company_id.in_((r.from_company_id, r.to_company_id)))
            .where(
                r.user_id == user_id,
                anchor.c.depth < CompanyRelationship.MAX_RELATIONSHIP_DEPTH,
            )
        )
        reachable = anchor.union_all(step)

        ranked = select(
            reachable.c.company_id,
            reachable.c.depth,
            reachable.c.path,
            func.row_number()
            .over(
                partition_by=reachable.c.company_id,
                order_by=[reachable.c.depth, reachable.c.path],
            )
            .label("rank"),
        ).subquery()

        stmt = (
            select(ranked.c.company_id, ranked.c.depth, ranked.c.path)
            .where(ranked.c.rank == 1)
            .order_by(ranked.c.depth, ranked.c.company_id)
            .limit(CompanyRelationship.MAX_RELATED_COMPANIES)
        )

        rows = (await db.execute(stmt)).all()
        return [RelatedCompany(row.company_id, row.depth, row.path) for row in rows]
