from typing import Annotated

from fastapi import APIRouter, Depends

from services.auth.api_key_service import ApiKeyService
from services.auth.dtos.api_key import (
    ApiKeyDto,
    CreateApiKeyRequest,
    CreateApiKeyResponse,
    ListApiKeysResponse,
)


class ApiKeysRouter:
    ApiKeyServiceDep = Annotated[ApiKeyService, Depends(ApiKeyService.get_with_deps)]

    def __init__(self) -> None:
        self.router = APIRouter(prefix="/api-keys", tags=["api-keys"])
        self._register()

    def _register(self) -> None:
        self.router.post("")(self.create_api_key)
        self.router.get("")(self.list_api_keys)
        self.router.delete("/{key_id}")(self.revoke_api_key)

    async def create_api_key(
        self, request: CreateApiKeyRequest, api_key_service: ApiKeyServiceDep
    ) -> CreateApiKeyResponse:
        """Mint a key. The secret in the response is not recoverable afterwards."""
        return await api_key_service.create(request)

    async def list_api_keys(
        self, api_key_service: ApiKeyServiceDep
    ) -> ListApiKeysResponse:
        return await api_key_service.list_keys()

    async def revoke_api_key(
        self, key_id: int, api_key_service: ApiKeyServiceDep
    ) -> ApiKeyDto:
        return await api_key_service.revoke(key_id)


router = ApiKeysRouter().router
