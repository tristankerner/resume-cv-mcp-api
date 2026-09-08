from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Index, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .base import SQAlchemyBase


class AuditLogEntry(SQAlchemyBase):
    """One row written by a database trigger - see the audit-log migration
    for the trigger bodies. No foreign keys on `row_user_id` or
    `actor_user_id`: the log must survive both the row and the user it
    refers to. Never written to by application code and never itself
    audited.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_table_row", "table_name", "row_pk", "changed_at"),
        Index("ix_audit_log_row_user", "row_user_id", "changed_at"),
        Index("ix_audit_log_actor", "actor_user_id", "changed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(nullable=False)
    row_pk: Mapped[str] = mapped_column(nullable=False)
    operation: Mapped[str] = mapped_column(nullable=False)
    changed_at: Mapped[datetime] = mapped_column(nullable=False)
    row_user_id: Mapped[int | None]
    actor_user_id: Mapped[int | None]
    actor_credential: Mapped[str | None]
    old_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    new_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    changed_columns: Mapped[list[str] | None] = mapped_column(JSON)

    def __repr__(self) -> str:
        return (
            f"AuditLogEntry(id={self.id!r}, table_name={self.table_name!r}, "
            f"row_pk={self.row_pk!r}, operation={self.operation!r})"
        )

    @staticmethod
    async def search(
        db: AsyncSession,
        row_user_id: int,
        table_name: str | None,
        row_pk: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[AuditLogEntry], int]:
        conditions = [AuditLogEntry.row_user_id == row_user_id]
        if table_name is not None:
            conditions.append(AuditLogEntry.table_name == table_name)
        if row_pk is not None:
            conditions.append(AuditLogEntry.row_pk == row_pk)

        total = (
            await db.execute(
                select(func.count()).select_from(AuditLogEntry).where(*conditions)
            )
        ).scalar_one()
        rows = list(
            (
                await db.execute(
                    select(AuditLogEntry)
                    .where(*conditions)
                    .order_by(AuditLogEntry.changed_at.desc(), AuditLogEntry.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return rows, total
