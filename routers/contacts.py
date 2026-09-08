from typing import Annotated

from fastapi import APIRouter, Depends, Query

from services.tracking.contact_service import ContactService
from services.tracking.dtos.common import ListEnvelope
from services.tracking.dtos.contact import (
    Contact,
    CreateContactRequest,
    UpdateContactRequest,
)

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


class ContactsRouter:
    ContactServiceDep = Annotated[ContactService, Depends(ContactService.get_with_deps)]

    def __init__(self) -> None:
        self.router = APIRouter(prefix="/contacts", tags=["tracking"])
        self._register()

    def _register(self) -> None:
        self.router.get("")(self.list_contacts)
        self.router.post("", status_code=201)(self.create_contact)
        self.router.get("/{contact_id}")(self.get_contact)
        self.router.patch("/{contact_id}")(self.update_contact)
        self.router.delete("/{contact_id}", status_code=204)(self.delete_contact)

    async def list_contacts(
        self,
        contact_service: ContactServiceDep,
        company_id: int | None = None,
        query: str | None = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> ListEnvelope[Contact]:
        return await contact_service.list_contacts(query, company_id, limit, offset)

    async def create_contact(
        self, request: CreateContactRequest, contact_service: ContactServiceDep
    ) -> Contact:
        return await contact_service.create_contact(request)

    async def get_contact(
        self, contact_id: int, contact_service: ContactServiceDep
    ) -> Contact:
        return await contact_service.get_contact(contact_id)

    async def update_contact(
        self,
        contact_id: int,
        request: UpdateContactRequest,
        contact_service: ContactServiceDep,
    ) -> Contact:
        return await contact_service.update_contact(contact_id, request)

    async def delete_contact(
        self, contact_id: int, contact_service: ContactServiceDep
    ) -> None:
        await contact_service.delete_contact(contact_id)
