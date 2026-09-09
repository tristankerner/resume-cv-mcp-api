from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.application import Application
from persistence.application_attachment import ApplicationAttachment
from persistence.application_event import ApplicationEvent
from persistence.base import Clock
from persistence.company import Company
from persistence.company_relationship import CompanyRelationship
from persistence.company_stack_item import CompanyStackItem
from persistence.contact import Contact as ContactRow
from services.auth.auth_service import AuthService
from services.auth.principal import Principal
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService
from services.tracking.application_view import ApplicationView
from services.tracking.contact_view import ContactView
from services.tracking.dtos.common import DuplicateCandidate, ListEnvelope
from services.tracking.dtos.company import (
    CompanyDetail,
    CompanyRelationshipDto,
    CompanyStackItemDto,
    CompanySummary,
    CreateCompanyRelationshipRequest,
    CreateCompanyRequest,
    CreateCompanyStackItemRequest,
    UpdateCompanyRelationshipRequest,
    UpdateCompanyRequest,
    UpdateCompanyStackItemRequest,
)
from services.tracking.duplicates import DuplicateFinder
from services.tracking.enums import (
    COMPANY_RELATIONSHIP_TYPE_LABELS,
    CompanyRelationshipType,
)
from services.tracking.exceptions import TrackingErrors
from services.tracking.normalization import Normalizer
from services.tracking.tracking_service import TrackingServiceBase

RECENT_APPLICATIONS_LIMIT = 20


class CompanyService(TrackingServiceBase):
    """Companies, their stack items and their inter-company relationships.
    See section 5/6 of the tracking plan."""

    @staticmethod
    def _pluralize(count: int, singular: str, plural: str) -> str:
        return singular if count == 1 else plural

    @staticmethod
    def _hint_for_counts(application_count: int, contact_count: int) -> str:
        if application_count == 0:
            return "no applications"
        noun = CompanyService._pluralize(
            application_count, "application", "applications"
        )
        return f"{application_count} {noun}"

    async def _candidates(
        self, name: str, normalized: str, exclude_id: int | None
    ) -> list[DuplicateCandidate]:
        haystack = [
            (company_id, display_name, normalized_name)
            for company_id, display_name, normalized_name in await Company.all_normalized_names(
                self.db, self._owner()
            )
            if company_id != exclude_id
        ]
        candidates = DuplicateFinder.rank(normalized, haystack)
        if not candidates:
            return candidates
        counts = await Company.counts_for(
            self.db, self._owner(), [candidate.id for candidate in candidates]
        )
        for candidate in candidates:
            application_count, _ = counts.get(candidate.id, (0, 0))
            candidate.hint = self._hint_for_counts(application_count, 0)
        return candidates

    def _refuse_if_duplicate(
        self, name: str, candidates: list[DuplicateCandidate]
    ) -> None:
        if not candidates:
            return
        noun = self._pluralize(len(candidates), "company", "companies")
        verb = self._pluralize(len(candidates), "looks", "look")
        message = (
            f'{len(candidates)} {noun} already {verb} like "{name}". Use one of '
            "them, or resend with confirm_create_duplicate: true."
        )
        raise TrackingErrors.duplicate("duplicate_company", message, candidates)

    async def _to_summary(self, company: Company) -> CompanySummary:
        counts = await Company.counts_for(self.db, self._owner(), [company.id])
        application_count, contact_count = counts.get(company.id, (0, 0))
        return CompanySummary(
            id=company.id,
            name=company.name,
            website=company.website,
            description=company.description,
            personal_note=company.personal_note,
            application_count=application_count,
            contact_count=contact_count,
            created_at=company.created_at,
            updated_at=company.updated_at,
        )

    async def _require_company(self, company_id: int) -> Company:
        company = await Company.get(self.db, self._owner(), company_id)
        if company is None:
            raise TrackingErrors.not_found("Company")
        return company

    async def list_companies(
        self, query: str | None, limit: int, offset: int, sort: str
    ) -> ListEnvelope[CompanySummary]:
        self._require(Scopes.COMPANIES_READ)
        owner = self._owner()
        rows, total = await Company.search(self.db, owner, query, limit, offset, sort)
        data = [
            CompanySummary(
                id=row.company.id,
                name=row.company.name,
                website=row.company.website,
                description=row.company.description,
                personal_note=row.company.personal_note,
                application_count=row.application_count,
                contact_count=row.contact_count,
                created_at=row.company.created_at,
                updated_at=row.company.updated_at,
            )
            for row in rows
        ]
        return ListEnvelope(data=data, total=total, limit=limit, offset=offset)

    async def get_company(self, company_id: int) -> CompanyDetail:
        self._require(Scopes.COMPANIES_READ)
        owner = self._owner()
        company = await self._require_company(company_id)
        summary = await self._to_summary(company)

        edges = await CompanyRelationship.list_for_company(self.db, owner, company_id)
        other_ids = list(
            {
                edge.to_company_id
                if edge.from_company_id == company_id
                else edge.from_company_id
                for edge in edges
            }
        )
        names = {company_id: company.name}
        names.update(await Company.names_for(self.db, owner, other_ids))
        relationships = [
            CompanyRelationshipDto(
                id=edge.id,
                from_company_id=edge.from_company_id,
                from_company_name=names.get(edge.from_company_id, ""),
                to_company_id=edge.to_company_id,
                to_company_name=names.get(edge.to_company_id, ""),
                type=edge.type,
                note=edge.note,
                created_at=edge.created_at,
            )
            for edge in edges
        ]

        stack_rows = await CompanyStackItem.list_for_company(self.db, owner, company_id)
        stack = [
            CompanyStackItemDto(
                id=item.id,
                company_id=item.company_id,
                name=item.name,
                type=item.type,
                description=item.description,
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
            for item in stack_rows
        ]

        contact_rows, _ = await ContactRow.search(
            self.db, owner, None, company_id, 1000, 0
        )
        contacts = [ContactView.of(row, company.name) for row in contact_rows]

        application_rows = await Application.list_for_company(
            self.db, owner, company_id, limit=RECENT_APPLICATIONS_LIMIT
        )
        # Grouped counts for the whole page, not a pair of queries per
        # application - same reasoning as `ApplicationService._to_summaries`.
        application_ids = [application.id for application in application_rows]
        event_counts = await ApplicationEvent.counts_for(
            self.db, owner, application_ids
        )
        attachment_counts = await ApplicationAttachment.counts_for(
            self.db, owner, application_ids
        )
        recent_applications = [
            ApplicationView.summary(
                application,
                company.name,
                event_counts.get(application.id, 0),
                attachment_counts.get(application.id, 0),
            )
            for application in application_rows
        ]

        return CompanyDetail(
            **summary.model_dump(),
            relationships=relationships,
            stack=stack,
            contacts=contacts,
            recent_applications=recent_applications,
        )

    async def create_company(self, request: CreateCompanyRequest) -> CompanySummary:
        self._require(Scopes.COMPANIES_WRITE)
        await self._begin_write()
        owner = self._owner()
        normalized = Normalizer.company_name(request.name)

        candidates = await self._candidates(request.name, normalized, None)
        has_exact = any(candidate.match == "exact" for candidate in candidates)
        if candidates and (has_exact or not request.confirm_create_duplicate):
            # An exact normalized-name match is refused unconditionally: the
            # unique index would reject the insert anyway, so there is
            # nothing for confirm_create_duplicate to override.
            self._refuse_if_duplicate(request.name, candidates)

        company = Company(
            user_id=owner,
            name=request.name,
            normalized_name=normalized,
            website=request.website,
            description=request.description,
            personal_note=request.personal_note,
        )
        self.db.add(company)
        await self.db.commit()
        return await self._to_summary(company)

    async def update_company(
        self, company_id: int, request: UpdateCompanyRequest
    ) -> CompanySummary:
        self._require(Scopes.COMPANIES_WRITE)
        await self._begin_write()
        company = await self._require_company(company_id)
        fields = request.model_fields_set

        if "name" in fields:
            if request.name is None:
                raise TrackingErrors.invalid_reference("name", "value")
            normalized = Normalizer.company_name(request.name)
            if normalized != company.normalized_name:
                candidates = await self._candidates(
                    request.name, normalized, company_id
                )
                has_exact = any(candidate.match == "exact" for candidate in candidates)
                if candidates and (has_exact or not request.confirm_create_duplicate):
                    self._refuse_if_duplicate(request.name, candidates)
                company.normalized_name = normalized
            company.name = request.name

        if "website" in fields:
            company.website = request.website
        if "description" in fields:
            company.description = request.description
        if "personal_note" in fields:
            company.personal_note = request.personal_note

        if self.db.is_modified(company, include_collections=False):
            company.updated_at = Clock.utcnow()
        await self.db.commit()
        return await self._to_summary(company)

    async def delete_company(self, company_id: int) -> None:
        self._require(Scopes.COMPANIES_DELETE)
        await self._begin_write()
        owner = self._owner()
        company = await self._require_company(company_id)

        application_count = (
            await Company.counts_for(self.db, owner, [company_id])
        ).get(company_id, (0, 0))[0]
        if application_count > 0:
            raise TrackingErrors.company_has_applications(application_count)

        for item in await CompanyStackItem.list_for_company(self.db, owner, company_id):
            await self.db.delete(item)
        for edge in await CompanyRelationship.list_for_company(
            self.db, owner, company_id
        ):
            await self.db.delete(edge)
        # An UPDATE rather than a page of rows mutated one at a time: the
        # paged version silently left the 1001st contact pointing at a company
        # that no longer exists, which SQLite would not catch because foreign
        # keys are off.
        await ContactRow.detach_from_company(self.db, owner, company_id)
        await self.db.delete(company)
        await self.db.commit()

    async def create_relationship(
        self, company_id: int, request: CreateCompanyRelationshipRequest
    ) -> CompanyRelationshipDto:
        self._require(Scopes.COMPANIES_WRITE)
        await self._begin_write()
        owner = self._owner()
        company = await self._require_company(company_id)

        if request.to_company_id == company_id:
            raise TrackingErrors.self_relationship()
        target = await Company.get(self.db, owner, request.to_company_id)
        if target is None:
            raise TrackingErrors.invalid_reference("to_company_id", "company")

        edges = await CompanyRelationship.list_for_company(self.db, owner, company_id)

        same_direction = [
            edge
            for edge in edges
            if edge.from_company_id == company_id
            and edge.to_company_id == request.to_company_id
            and edge.type == request.type
        ]
        if same_direction:
            raise TrackingErrors.relationship_exists()

        # The inverse direction, same or paired type, already states this
        # fact - "Acme parent_of Globex" when "Globex child_of Acme" exists
        # describes one relationship twice. Checked after the exact-repeat
        # check above, which has its own, more specific message.
        #
        # `.get`, not `[...]`: the directional types that have no reverse
        # term in the enum are absent from INVERSES on purpose, and get no
        # inverse check rather than a KeyError. See that dict's comment.
        inverse_type = CompanyRelationshipType.INVERSES.get(request.type.value)
        inverse = [
            edge
            for edge in edges
            if inverse_type is not None
            and edge.from_company_id == request.to_company_id
            and edge.to_company_id == company_id
            and edge.type == inverse_type
        ]
        if inverse:
            existing_label = COMPANY_RELATIONSHIP_TYPE_LABELS[
                CompanyRelationshipType(inverse[0].type)
            ]
            raise TrackingErrors.relationship_exists(
                f'That relationship already exists: "{target.name}" is '
                f'{existing_label} "{company.name}".'
            )

        edge = CompanyRelationship(
            user_id=owner,
            from_company_id=company_id,
            to_company_id=request.to_company_id,
            type=request.type,
            note=request.note,
        )
        self.db.add(edge)
        await self.db.commit()
        return CompanyRelationshipDto(
            id=edge.id,
            from_company_id=edge.from_company_id,
            from_company_name=company.name,
            to_company_id=edge.to_company_id,
            to_company_name=target.name,
            type=edge.type,
            note=edge.note,
            created_at=edge.created_at,
        )

    async def _require_relationship(self, relationship_id: int) -> CompanyRelationship:
        edge = await CompanyRelationship.get(self.db, self._owner(), relationship_id)
        if edge is None:
            raise TrackingErrors.not_found("Company relationship")
        return edge

    async def update_relationship(
        self, relationship_id: int, request: UpdateCompanyRelationshipRequest
    ) -> CompanyRelationshipDto:
        self._require(Scopes.COMPANIES_WRITE)
        await self._begin_write()
        owner = self._owner()
        edge = await self._require_relationship(relationship_id)
        fields = request.model_fields_set
        if "type" in fields and request.type is not None:
            edge.type = request.type
        if "note" in fields:
            edge.note = request.note
        await self.db.commit()

        from_company = await Company.get(self.db, owner, edge.from_company_id)
        to_company = await Company.get(self.db, owner, edge.to_company_id)
        return CompanyRelationshipDto(
            id=edge.id,
            from_company_id=edge.from_company_id,
            from_company_name=from_company.name if from_company else "",
            to_company_id=edge.to_company_id,
            to_company_name=to_company.name if to_company else "",
            type=edge.type,
            note=edge.note,
            created_at=edge.created_at,
        )

    async def delete_relationship(self, relationship_id: int) -> None:
        self._require(Scopes.COMPANIES_DELETE)
        await self._begin_write()
        edge = await self._require_relationship(relationship_id)
        await self.db.delete(edge)
        await self.db.commit()

    async def list_stack(self, company_id: int) -> ListEnvelope[CompanyStackItemDto]:
        self._require(Scopes.COMPANIES_READ)
        owner = self._owner()
        await self._require_company(company_id)
        rows = await CompanyStackItem.list_for_company(self.db, owner, company_id)
        data = [
            CompanyStackItemDto(
                id=item.id,
                company_id=item.company_id,
                name=item.name,
                type=item.type,
                description=item.description,
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
            for item in rows
        ]
        return ListEnvelope(data=data, total=len(data), limit=len(data), offset=0)

    async def create_stack_item(
        self, company_id: int, request: CreateCompanyStackItemRequest
    ) -> CompanyStackItemDto:
        self._require(Scopes.COMPANIES_WRITE)
        await self._begin_write()
        owner = self._owner()
        await self._require_company(company_id)
        normalized = Normalizer.stack_item(request.name)

        haystack = await CompanyStackItem.all_normalized_for_company(
            self.db, owner, company_id
        )
        candidates = DuplicateFinder.rank(normalized, haystack)
        has_exact = any(candidate.match == "exact" for candidate in candidates)
        if candidates and (has_exact or not request.confirm_create_duplicate):
            noun = self._pluralize(len(candidates), "item", "items")
            message = (
                f'{len(candidates)} stack {noun} already look like "{request.name}". '
                "Use one of them, or resend with confirm_create_duplicate: true."
            )
            raise TrackingErrors.duplicate("duplicate_stack_item", message, candidates)

        item = CompanyStackItem(
            user_id=owner,
            company_id=company_id,
            name=request.name,
            normalized_name=normalized,
            type=request.type,
            description=request.description,
        )
        self.db.add(item)
        await self.db.commit()
        return CompanyStackItemDto(
            id=item.id,
            company_id=item.company_id,
            name=item.name,
            type=item.type,
            description=item.description,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    async def _require_stack_item(self, item_id: int) -> CompanyStackItem:
        item = await CompanyStackItem.get(self.db, self._owner(), item_id)
        if item is None:
            raise TrackingErrors.not_found("Stack item")
        return item

    async def update_stack_item(
        self, item_id: int, request: UpdateCompanyStackItemRequest
    ) -> CompanyStackItemDto:
        self._require(Scopes.COMPANIES_WRITE)
        await self._begin_write()
        owner = self._owner()
        item = await self._require_stack_item(item_id)
        fields = request.model_fields_set

        if "name" in fields and request.name is not None:
            normalized = Normalizer.stack_item(request.name)
            if normalized != item.normalized_name:
                existing = await CompanyStackItem.get_by_normalized_name(
                    self.db, owner, item.company_id, normalized
                )
                if existing is not None and existing.id != item_id:
                    raise TrackingErrors.duplicate(
                        "duplicate_stack_item",
                        f'A stack item named "{existing.name}" already exists.',
                        [
                            DuplicateCandidate(
                                id=existing.id,
                                label=existing.name,
                                match="exact",
                                score=1.0,
                            )
                        ],
                    )
                item.normalized_name = normalized
            item.name = request.name
        if "type" in fields and request.type is not None:
            item.type = request.type
        if "description" in fields:
            item.description = request.description

        if self.db.is_modified(item, include_collections=False):
            item.updated_at = Clock.utcnow()
        await self.db.commit()
        return CompanyStackItemDto(
            id=item.id,
            company_id=item.company_id,
            name=item.name,
            type=item.type,
            description=item.description,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    async def delete_stack_item(self, item_id: int) -> None:
        self._require(Scopes.COMPANIES_DELETE)
        await self._begin_write()
        item = await self._require_stack_item(item_id)
        await self.db.delete(item)
        await self.db.commit()

    @staticmethod
    def get_with_deps(
        db: Annotated[AsyncSession, Depends(DatabaseService.get_async_db_session)],
        principal: Annotated[
            Principal | None, Depends(AuthService.get_optional_principal)
        ],
    ) -> CompanyService:
        return CompanyService(db, principal)
