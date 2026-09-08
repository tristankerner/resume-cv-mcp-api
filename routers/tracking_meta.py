from typing import Annotated

from fastapi import APIRouter, Depends

from services.tracking.dtos.enums import EnumsResponse
from services.tracking.tracking_meta_service import TrackingMetaService


class TrackingMetaRouter:
    """Its own small router rather than a method tacked onto `CompaniesRouter`
    - `GET /tracking/enums` belongs to no one entity, so it should not look
    like it lives on companies."""

    TrackingMetaServiceDep = Annotated[
        TrackingMetaService, Depends(TrackingMetaService.get_with_deps)
    ]

    def __init__(self) -> None:
        self.router = APIRouter(prefix="/tracking", tags=["tracking"])
        self._register()

    def _register(self) -> None:
        self.router.get("/enums")(self.get_enums)

    async def get_enums(
        self, tracking_meta_service: TrackingMetaServiceDep
    ) -> EnumsResponse:
        return await tracking_meta_service.get_enums()
