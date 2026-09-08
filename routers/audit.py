from typing import Annotated

from fastapi import APIRouter, Depends, Query

from services.tracking.audit_service import AuditService
from services.tracking.dtos.audit import AuditEntry
from services.tracking.dtos.common import ListEnvelope

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


class AuditRouter:
    AuditServiceDep = Annotated[AuditService, Depends(AuditService.get_with_deps)]

    def __init__(self) -> None:
        self.router = APIRouter(prefix="/audit", tags=["tracking"])
        self._register()

    def _register(self) -> None:
        self.router.get("")(self.search)

    async def search(
        self,
        audit_service: AuditServiceDep,
        table: str | None = None,
        row_id: str | None = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> ListEnvelope[AuditEntry]:
        return await audit_service.search(table, row_id, limit, offset)
