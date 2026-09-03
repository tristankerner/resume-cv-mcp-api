from typing import Any

from sqlalchemy import JSON, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .base import SQAlchemyBase


class DocumentSchema(SQAlchemyBase):
    """One version of one document type's shape: the JSON Schema it validated
    against and the fictional example that shipped beside it.

    Keyed `(version, document_type)` rather than a surrogate id so "starts at
    schema 1" is literally true for every type — see
    `SCHEMA_VERSIONING_PLAN.md` decision 2. Rows are appended by migrations
    only; nothing in `services/` writes one.
    """

    __tablename__ = "document_schema"

    version: Mapped[int] = mapped_column(primary_key=True, nullable=False)
    document_type: Mapped[str] = mapped_column(primary_key=True, nullable=False)
    description: Mapped[str] = mapped_column(nullable=False)
    json_schema: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    example: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    def __repr__(self) -> str:
        return (
            f"DocumentSchema(version={self.version!r}, "
            f"document_type={self.document_type!r})"
        )

    @staticmethod
    async def latest_per_type(db: AsyncSession) -> list[DocumentSchema]:
        """One row per document type: its highest version. What a newly
        registered account is seeded from — see services/user/document_seeder.py."""
        latest = (
            select(
                DocumentSchema.document_type,
                func.max(DocumentSchema.version).label("version"),
            )
            .group_by(DocumentSchema.document_type)
            .subquery()
        )
        stmt = select(DocumentSchema).join(
            latest,
            (DocumentSchema.document_type == latest.c.document_type)
            & (DocumentSchema.version == latest.c.version),
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())
