from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from services.tracking.company_service import CompanyService
from services.tracking.dtos.common import ListEnvelope
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

CompanySort = Literal["name", "-name", "created_at", "-created_at"]
Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


class CompaniesRouter:
    """Companies, their relationships and their stack items - see API
    contract 14.5. No prefix: paths span `/companies`, `/company-relationships`
    and `/company-stack`."""

    CompanyServiceDep = Annotated[CompanyService, Depends(CompanyService.get_with_deps)]

    def __init__(self) -> None:
        self.router = APIRouter(tags=["tracking"])
        self._register()

    def _register(self) -> None:
        self.router.get("/companies")(self.list_companies)
        self.router.post("/companies", status_code=201)(self.create_company)
        self.router.get("/companies/{company_id}")(self.get_company)
        self.router.patch("/companies/{company_id}")(self.update_company)
        self.router.delete("/companies/{company_id}", status_code=204)(
            self.delete_company
        )
        self.router.post("/companies/{company_id}/relationships", status_code=201)(
            self.create_relationship
        )
        self.router.get("/companies/{company_id}/stack")(self.list_stack)
        self.router.post("/companies/{company_id}/stack", status_code=201)(
            self.create_stack_item
        )
        self.router.patch("/company-relationships/{relationship_id}")(
            self.update_relationship
        )
        self.router.delete("/company-relationships/{relationship_id}", status_code=204)(
            self.delete_relationship
        )
        self.router.patch("/company-stack/{item_id}")(self.update_stack_item)
        self.router.delete("/company-stack/{item_id}", status_code=204)(
            self.delete_stack_item
        )

    async def list_companies(
        self,
        company_service: CompanyServiceDep,
        query: str | None = None,
        limit: Limit = 50,
        offset: Offset = 0,
        sort: CompanySort = "name",
    ) -> ListEnvelope[CompanySummary]:
        return await company_service.list_companies(query, limit, offset, sort)

    async def create_company(
        self, request: CreateCompanyRequest, company_service: CompanyServiceDep
    ) -> CompanySummary:
        return await company_service.create_company(request)

    async def get_company(
        self, company_id: int, company_service: CompanyServiceDep
    ) -> CompanyDetail:
        return await company_service.get_company(company_id)

    async def update_company(
        self,
        company_id: int,
        request: UpdateCompanyRequest,
        company_service: CompanyServiceDep,
    ) -> CompanySummary:
        return await company_service.update_company(company_id, request)

    async def delete_company(
        self, company_id: int, company_service: CompanyServiceDep
    ) -> None:
        await company_service.delete_company(company_id)

    async def create_relationship(
        self,
        company_id: int,
        request: CreateCompanyRelationshipRequest,
        company_service: CompanyServiceDep,
    ) -> CompanyRelationshipDto:
        return await company_service.create_relationship(company_id, request)

    async def update_relationship(
        self,
        relationship_id: int,
        request: UpdateCompanyRelationshipRequest,
        company_service: CompanyServiceDep,
    ) -> CompanyRelationshipDto:
        return await company_service.update_relationship(relationship_id, request)

    async def delete_relationship(
        self, relationship_id: int, company_service: CompanyServiceDep
    ) -> None:
        await company_service.delete_relationship(relationship_id)

    async def list_stack(
        self, company_id: int, company_service: CompanyServiceDep
    ) -> ListEnvelope[CompanyStackItemDto]:
        return await company_service.list_stack(company_id)

    async def create_stack_item(
        self,
        company_id: int,
        request: CreateCompanyStackItemRequest,
        company_service: CompanyServiceDep,
    ) -> CompanyStackItemDto:
        return await company_service.create_stack_item(company_id, request)

    async def update_stack_item(
        self,
        item_id: int,
        request: UpdateCompanyStackItemRequest,
        company_service: CompanyServiceDep,
    ) -> CompanyStackItemDto:
        return await company_service.update_stack_item(item_id, request)

    async def delete_stack_item(
        self, item_id: int, company_service: CompanyServiceDep
    ) -> None:
        await company_service.delete_stack_item(item_id)
