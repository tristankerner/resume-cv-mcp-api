from datetime import date, datetime
from typing import Any, NamedTuple

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    exists,
    func,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, Mapped, mapped_column

from .base import Clock, SQAlchemyBase
from .batch_count import BatchCount
from .page import Page
from .related_count import RelatedCount


class Application(SQAlchemyBase):
    """One job application. Always to a company - see `company_id` below and
    `services/tracking/company_service.py`'s 409 on deleting a company that
    still has applications.

    The three `<type>_document_name` / `<type>_revision_id` pairs are
    composite foreign keys into `documents(created_by, name, revision_id)`
    with no `ON DELETE` clause - see `services/document/document_service.py`
    for the delete/rename handling that keeps them consistent by hand.
    """

    __tablename__ = "applications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "resume_document_name", "resume_revision_id"],
            ["documents.created_by", "documents.name", "documents.revision_id"],
            name="fk_applications_resume_revision",
            # Deferred so the rename path can retarget an application and
            # clone the document's revisions in either order within one
            # transaction - see alembic 9c14ab30f6d2. Postgres only; SQLite
            # does not enforce foreign keys here at all.
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["user_id", "metadata_document_name", "metadata_revision_id"],
            ["documents.created_by", "documents.name", "documents.revision_id"],
            name="fk_applications_metadata_revision",
            # Deferred so the rename path can retarget an application and
            # clone the document's revisions in either order within one
            # transaction - see alembic 9c14ab30f6d2. Postgres only; SQLite
            # does not enforce foreign keys here at all.
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["user_id", "skill_document_name", "skill_revision_id"],
            ["documents.created_by", "documents.name", "documents.revision_id"],
            name="fk_applications_skill_revision",
            # Deferred so the rename path can retarget an application and
            # clone the document's revisions in either order within one
            # transaction - see alembic 9c14ab30f6d2. Postgres only; SQLite
            # does not enforce foreign keys here at all.
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "(resume_document_name IS NULL) = (resume_revision_id IS NULL)",
            name="ck_applications_resume_ref_complete",
        ),
        CheckConstraint(
            "(metadata_document_name IS NULL) = (metadata_revision_id IS NULL)",
            name="ck_applications_metadata_ref_complete",
        ),
        CheckConstraint(
            "(skill_document_name IS NULL) = (skill_revision_id IS NULL)",
            name="ck_applications_skill_ref_complete",
        ),
        Index("ix_applications_user_submitted", "user_id", "date_submitted"),
        Index("ix_applications_user_company", "user_id", "company_id"),
        Index("ix_applications_user_status", "user_id", "status"),
        Index("ix_applications_user_job_code", "user_id", "normalized_job_code"),
        Index("ix_applications_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    url: Mapped[str | None]
    job_title: Mapped[str | None]
    normalized_job_title: Mapped[str | None]
    job_code: Mapped[str | None]
    # The code with separators and case removed - see `Normalizer.job_code`.
    # NULL when there is no code, or when it is too short to match on.
    normalized_job_code: Mapped[str | None]
    resume_document_name: Mapped[str | None]
    resume_revision_id: Mapped[int | None]
    metadata_document_name: Mapped[str | None]
    metadata_revision_id: Mapped[int | None]
    skill_document_name: Mapped[str | None]
    skill_revision_id: Mapped[int | None]
    resume_label: Mapped[str | None]
    initial_prompt_text: Mapped[str | None] = mapped_column(Text)
    job_description: Mapped[str | None] = mapped_column(Text)
    date_submitted: Mapped[date | None]
    manually_modified: Mapped[bool] = mapped_column(default=False, nullable=False)
    modification_note: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None]
    system: Mapped[str | None]
    status: Mapped[str] = mapped_column(nullable=False)
    status_changed_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=Clock.utcnow, nullable=False)

    def __repr__(self) -> str:
        return (
            f"Application(id={self.id!r}, user_id={self.user_id!r}, "
            f"company_id={self.company_id!r}, status={self.status!r})"
        )

    @classmethod
    def heavy_columns(cls) -> tuple[InstrumentedAttribute[Any], ...]:
        """The three a summary never shows. `ApplicationDetail` reads all of
        them, so `Application.get` deliberately does not use `light`."""
        return (cls.initial_prompt_text, cls.job_description, cls.modification_note)

    @staticmethod
    async def get(
        db: AsyncSession, user_id: int, application_id: int
    ) -> Application | None:
        return (
            (
                await db.execute(
                    select(Application).where(
                        Application.id == application_id,
                        Application.user_id == user_id,
                    )
                )
            )
            .scalars()
            .first()
        )

    @staticmethod
    async def list_for_company(
        db: AsyncSession, user_id: int, company_id: int, limit: int | None = None
    ) -> list[Application]:
        """Newest `date_submitted` first, NULLs last - the shape
        `recent_applications` on a company detail wants."""
        stmt = Application.light(
            select(Application)
            .where(Application.user_id == user_id, Application.company_id == company_id)
            .order_by(
                Application.date_submitted.desc().nulls_last(), Application.id.desc()
            )
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await db.execute(stmt)).scalars().all())

    @staticmethod
    async def sharing_job_code(
        db: AsyncSession, user_id: int, normalized_job_code: str, exclude_id: int | None
    ) -> list[Application]:
        """This owner's other applications carrying the same job code.

        Deliberately not filtered by company: the case this exists for is two
        recruiters — often two different agencies, and so two different
        company rows — putting the same requisition forward.
        """
        stmt = Application.light(
            select(Application).where(
                Application.user_id == user_id,
                Application.normalized_job_code == normalized_job_code,
            )
        )
        if exclude_id is not None:
            stmt = stmt.where(Application.id != exclude_id)
        stmt = stmt.order_by(
            Application.date_submitted.desc().nulls_last(), Application.id.desc()
        )
        return list((await db.execute(stmt)).scalars().all())

    @staticmethod
    async def job_code_match_counts(
        db: AsyncSession, user_id: int, normalized_job_codes: list[str]
    ) -> dict[str, int]:
        """How many of this owner's applications carry each code, in one
        grouped query. The caller subtracts the row itself to get "how many
        *others*"."""
        codes = [code for code in set(normalized_job_codes) if code]
        return await BatchCount.for_keys(
            db,
            key_column=Application.normalized_job_code,
            owner_column=Application.user_id,
            owner_id=user_id,
            keys=codes,
        )

    @staticmethod
    async def search(
        db: AsyncSession,
        user_id: int,
        *,
        company_id: int | None = None,
        statuses: list[str] | None = None,
        source: str | None = None,
        system: str | None = None,
        query: str | None = None,
        job_code: str | None = None,
        submitted_from: date | None = None,
        submitted_to: date | None = None,
        has_attachments: bool | None = None,
        sort: str = "-date_submitted",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ApplicationSearchRow], int]:
        from .application_attachment import ApplicationAttachment
        from .application_event import ApplicationEvent
        from .company import Company

        conditions = [Application.user_id == user_id]
        if company_id is not None:
            conditions.append(Application.company_id == company_id)
        if statuses:
            conditions.append(Application.status.in_(statuses))
        if source is not None:
            conditions.append(Application.source == source)
        if system is not None:
            conditions.append(Application.system == system)
        if job_code is not None:
            # Matched on the normalized form, so `?job_code=req-12345` finds a
            # row stored as `REQ 12345`.
            conditions.append(Application.normalized_job_code == job_code)
        if submitted_from is not None:
            conditions.append(Application.date_submitted >= submitted_from)
        if submitted_to is not None:
            conditions.append(Application.date_submitted <= submitted_to)
        if has_attachments is not None:
            attachment_exists = exists(
                select(1).where(ApplicationAttachment.application_id == Application.id)
            )
            conditions.append(
                attachment_exists if has_attachments else ~attachment_exists
            )

        def _base(select_clause):
            # Always joined, not just when the sort or query needs it: every
            # row's company name now rides along in the same query - see
            # ApplicationSearchRow. `company_id` is a NOT NULL FK, so an
            # inner join can never drop a row or change a count.
            stmt = select_clause.where(*conditions).join(
                Company, Company.id == Application.company_id
            )
            if query:
                pattern = f"%{query.lower()}%"
                stmt = stmt.where(
                    func.lower(func.coalesce(Application.job_title, "")).like(pattern)
                    | func.lower(Company.name).like(pattern)
                )
            return stmt

        if sort == "company":
            order = (Company.name.asc(),)
        elif sort == "-company":
            order = (Company.name.desc(),)
        else:
            order_by = {
                "-date_submitted": (Application.date_submitted.desc().nulls_last(),),
                "date_submitted": (Application.date_submitted.asc().nulls_last(),),
                "status": (Application.status.asc(),),
                "-created_at": (Application.created_at.desc(),),
                "created_at": (Application.created_at.asc(),),
            }
            order = order_by.get(sort, order_by["-date_submitted"])

        row_stmt = Application.light(
            _base(
                select(
                    Application,
                    Company.name,
                    RelatedCount.column(
                        child_owner=ApplicationEvent.user_id,
                        child_parent=ApplicationEvent.application_id,
                        parent_key=Application.id,
                        owner_id=user_id,
                        label="event_count",
                    ),
                    RelatedCount.column(
                        child_owner=ApplicationAttachment.user_id,
                        child_parent=ApplicationAttachment.application_id,
                        parent_key=Application.id,
                        owner_id=user_id,
                        label="attachment_count",
                    ),
                )
            )
        ).order_by(*order, Application.id.desc())
        total_stmt = _base(select(func.count(Application.id)))
        rows, total = await Page.fetch(
            db, row_stmt, limit=limit, offset=offset, total_stmt=total_stmt
        )
        return [ApplicationSearchRow(*row) for row in rows], total

    @staticmethod
    async def candidates_for_dedup(
        db: AsyncSession,
        user_id: int,
        company_id: int,
        normalized_job_title: str | None,
    ) -> list[tuple[int, str, str, date | None]]:
        """`(id, job_title, normalized_job_title, date_submitted)` for the
        caller's other applications to this company, restricted to those that
        actually have a normalized title to compare - a title-less posting
        cannot be ranked against."""
        if normalized_job_title is None:
            return []
        rows = (
            await db.execute(
                select(
                    Application.id,
                    Application.job_title,
                    Application.normalized_job_title,
                    Application.date_submitted,
                ).where(
                    Application.user_id == user_id,
                    Application.company_id == company_id,
                    Application.normalized_job_title.is_not(None),
                )
            )
        ).all()
        return [
            (row.id, row.job_title or "", row.normalized_job_title, row.date_submitted)
            for row in rows
        ]

    @staticmethod
    async def clear_document_references(
        db: AsyncSession, owner_id: int, document_name: str
    ) -> None:
        """Null out every `<type>_document_name`/`<type>_revision_id` pair
        pointing at `document_name`, across all three slots, for one owner's
        applications. Does not commit - the caller (document_service) owns
        the transaction. See section 4.8 of the tracking plan.
        """
        result = await db.execute(
            Application.light(
                select(Application).where(
                    Application.user_id == owner_id,
                    or_(
                        Application.resume_document_name == document_name,
                        Application.metadata_document_name == document_name,
                        Application.skill_document_name == document_name,
                    ),
                )
            )
        )
        for application in result.scalars().all():
            if application.resume_document_name == document_name:
                if application.resume_label is None:
                    application.resume_label = document_name
                application.resume_document_name = None
                application.resume_revision_id = None
            if application.metadata_document_name == document_name:
                application.metadata_document_name = None
                application.metadata_revision_id = None
            if application.skill_document_name == document_name:
                application.skill_document_name = None
                application.skill_revision_id = None

    @staticmethod
    async def retarget_document_references(
        db: AsyncSession, owner_id: int, old_name: str, new_name: str
    ) -> None:
        """Move every `<type>_document_name` pointing at `old_name` to
        `new_name`. Revision ids do not change during a rename. Does not
        commit - see `clear_document_references`."""
        result = await db.execute(
            Application.light(
                select(Application).where(
                    Application.user_id == owner_id,
                    or_(
                        Application.resume_document_name == old_name,
                        Application.metadata_document_name == old_name,
                        Application.skill_document_name == old_name,
                    ),
                )
            )
        )
        for application in result.scalars().all():
            if application.resume_document_name == old_name:
                application.resume_document_name = new_name
            if application.metadata_document_name == old_name:
                application.metadata_document_name = new_name
            if application.skill_document_name == old_name:
                application.skill_document_name = new_name


class ApplicationSearchRow(NamedTuple):
    """One row of `Application.search`'s result: the entity plus the fields
    a summary needs that would otherwise cost a lookup of their own - a
    company name and two `RelatedCount` columns. See
    `QUERY_PERFORMANCE_PLAN.md` Phase 4. `job_code_match_count` is
    deliberately not here - it counts siblings across the whole table
    rather than children of this row, so it stays a grouped follow-up in
    `Application.job_code_match_counts`.
    """

    application: Application
    company_name: str
    event_count: int
    attachment_count: int
