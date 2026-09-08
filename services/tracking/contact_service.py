from typing import Annotated

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.application_event import ApplicationEvent
from persistence.base import Clock
from persistence.company import Company
from persistence.contact import Contact
from services.auth.auth_service import AuthService
from services.auth.principal import Principal
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService
from services.tracking.contact_view import ContactView
from services.tracking.dtos.common import DuplicateCandidate, ListEnvelope
from services.tracking.dtos.contact import (
    Contact as ContactDto,
)
from services.tracking.dtos.contact import (
    CreateContactRequest,
    UpdateContactRequest,
)
from services.tracking.duplicates import DuplicateFinder
from services.tracking.exceptions import TrackingErrors
from services.tracking.normalization import Normalizer
from services.tracking.tracking_service import TrackingServiceBase


class ContactService(TrackingServiceBase):
    """Contacts and their dedup rule: exact normalized email, or a fuzzy
    match on person name within the same company. See section 5.3."""

    @staticmethod
    def _label(first_name: str | None, last_name: str | None, email: str | None) -> str:
        name = " ".join(part for part in (first_name, last_name) if part).strip()
        return name or email or "(unnamed contact)"

    async def _company_id_or_404(self, company_id: int | None) -> None:
        if company_id is None:
            return
        company = await Company.get(self.db, self._owner(), company_id)
        if company is None:
            raise TrackingErrors.invalid_reference("company_id", "company")

    async def _company_name(self, company_id: int | None) -> str | None:
        if company_id is None:
            return None
        company = await Company.get(self.db, self._owner(), company_id)
        return company.name if company is not None else None

    async def _candidates(
        self,
        first_name: str | None,
        last_name: str | None,
        email: str | None,
        company_id: int | None,
        exclude_id: int | None,
    ) -> list[DuplicateCandidate]:
        owner = self._owner()
        candidates: list[DuplicateCandidate] = []
        seen: set[int] = set()

        normalized_email = Normalizer.email(email)
        if normalized_email:
            existing = await Contact.get_by_email(self.db, owner, normalized_email)
            if existing is not None and existing.id != exclude_id:
                candidates.append(
                    DuplicateCandidate(
                        id=existing.id,
                        label=self._label(
                            existing.first_name, existing.last_name, existing.email
                        ),
                        match="exact",
                        score=1.0,
                    )
                )
                seen.add(existing.id)

        normalized_name = Normalizer.person_name(first_name, last_name)
        if normalized_name:
            haystack = [
                (contact_id, label, normalized)
                for contact_id, label, normalized in await Contact.candidates_for_dedup(
                    self.db, owner, company_id
                )
                if contact_id != exclude_id and contact_id not in seen
            ]
            candidates.extend(DuplicateFinder.rank(normalized_name, haystack))

        candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        return candidates[: DuplicateFinder.MAX_RESULTS]

    def _refuse_if_duplicate(self, candidates: list[DuplicateCandidate]) -> None:
        if not candidates:
            return
        message = (
            f"{len(candidates)} contact(s) already look like this one. Use one of "
            "them, or resend with confirm_create_duplicate: true."
        )
        raise TrackingErrors.duplicate("duplicate_contact", message, candidates)

    async def _to_dto(self, contact: Contact) -> ContactDto:
        return ContactView.of(contact, await self._company_name(contact.company_id))

    async def _require_contact(self, contact_id: int) -> Contact:
        contact = await Contact.get(self.db, self._owner(), contact_id)
        if contact is None:
            raise TrackingErrors.not_found("Contact")
        return contact

    async def list_contacts(
        self, query: str | None, company_id: int | None, limit: int, offset: int
    ) -> ListEnvelope[ContactDto]:
        self._require(Scopes.CONTACTS_READ)
        owner = self._owner()
        rows, total = await Contact.search(
            self.db, owner, query, company_id, limit, offset
        )
        data = [await self._to_dto(row) for row in rows]
        return ListEnvelope(data=data, total=total, limit=limit, offset=offset)

    async def get_contact(self, contact_id: int) -> ContactDto:
        self._require(Scopes.CONTACTS_READ)
        contact = await self._require_contact(contact_id)
        return await self._to_dto(contact)

    async def create_contact(self, request: CreateContactRequest) -> ContactDto:
        self._require(Scopes.CONTACTS_WRITE)
        await self._begin_write()
        owner = self._owner()

        if not any((request.first_name, request.last_name, request.email)):
            raise TrackingErrors.contact_needs_identity()
        await self._company_id_or_404(request.company_id)

        if not request.confirm_create_duplicate:
            candidates = await self._candidates(
                request.first_name,
                request.last_name,
                request.email,
                request.company_id,
                None,
            )
            self._refuse_if_duplicate(candidates)

        contact = Contact(
            user_id=owner,
            company_id=request.company_id,
            first_name=request.first_name,
            last_name=request.last_name,
            normalized_name=Normalizer.person_name(
                request.first_name, request.last_name
            ),
            email=Normalizer.email(request.email),
            phone=request.phone,
            description=request.description,
            personal_note=request.personal_note,
            rating=request.rating,
        )
        self.db.add(contact)
        await self.db.commit()
        return await self._to_dto(contact)

    async def update_contact(
        self, contact_id: int, request: UpdateContactRequest
    ) -> ContactDto:
        self._require(Scopes.CONTACTS_WRITE)
        await self._begin_write()
        contact = await self._require_contact(contact_id)
        fields = request.model_fields_set

        if "company_id" in fields:
            await self._company_id_or_404(request.company_id)
            contact.company_id = request.company_id
        if "first_name" in fields:
            contact.first_name = request.first_name
        if "last_name" in fields:
            contact.last_name = request.last_name
        if "first_name" in fields or "last_name" in fields:
            contact.normalized_name = Normalizer.person_name(
                contact.first_name, contact.last_name
            )
        if "email" in fields:
            contact.email = Normalizer.email(request.email)
        if "phone" in fields:
            contact.phone = request.phone
        if "description" in fields:
            contact.description = request.description
        if "personal_note" in fields:
            contact.personal_note = request.personal_note
        if "rating" in fields:
            contact.rating = request.rating

        if not any((contact.first_name, contact.last_name, contact.email)):
            raise TrackingErrors.contact_needs_identity()

        if self.db.is_modified(contact, include_collections=False):
            contact.updated_at = Clock.utcnow()
        await self.db.commit()
        return await self._to_dto(contact)

    async def delete_contact(self, contact_id: int) -> None:
        self._require(Scopes.CONTACTS_DELETE)
        await self._begin_write()
        owner = self._owner()
        contact = await self._require_contact(contact_id)

        result = await self.db.execute(
            select(ApplicationEvent).where(
                ApplicationEvent.user_id == owner,
                ApplicationEvent.contact_id == contact_id,
            )
        )
        for event in result.scalars().all():
            event.contact_id = None

        await self.db.delete(contact)
        await self.db.commit()

    @staticmethod
    def get_with_deps(
        db: Annotated[AsyncSession, Depends(DatabaseService.get_async_db_session)],
        principal: Annotated[
            Principal | None, Depends(AuthService.get_optional_principal)
        ],
    ) -> ContactService:
        return ContactService(db, principal)
