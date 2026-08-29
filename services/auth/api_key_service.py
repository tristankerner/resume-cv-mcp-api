from datetime import timedelta
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.api_key import ApiKey
from persistence.base import utcnow
from persistence.user import User
from services.auth.api_keys import ApiKeyToken
from services.auth.auth_service import AuthService
from services.auth.dtos.api_key import (
    ApiKeyDto,
    CreateApiKeyRequest,
    CreateApiKeyResponse,
    ListApiKeysResponse,
)
from services.auth.exceptions import AuthErrors
from services.auth.principal import Principal
from services.auth.scopes import ScopeResolver, Scopes
from services.database.database_service import DatabaseService
from services.service_interface import ServiceProviderInterface
from services.user.exceptions import UserErrors


class ApiKeyService(ServiceProviderInterface):
    def __init__(self, db: AsyncSession, principal: Principal):
        self.db: AsyncSession = db
        self.principal: Principal = principal

    def _resolve_owner_id(self, requested: int | None) -> int:
        owner_id = requested if requested is not None else self.principal.user_id
        if owner_id != self.principal.user_id:
            self.principal.require_scope(Scopes.USERS_ADMIN)
        return owner_id

    async def create(self, request: CreateApiKeyRequest) -> CreateApiKeyResponse:
        self.principal.require_interactive()
        owner_id = self._resolve_owner_id(request.user_id)

        owner = await User.get_user_by_id(self.db, owner_id)
        if owner is None:
            raise UserErrors.not_found()

        # The ceiling is the owner's roles, not the caller's. An admin minting
        # for someone else cannot hand out more than that user already has.
        grantable = await ScopeResolver.for_roles(self.db, owner.roles)
        excess = frozenset(request.scopes) - grantable
        if excess:
            raise AuthErrors.scopes_exceed_owner([str(scope) for scope in excess])

        full_key, prefix, key_hash = ApiKeyToken.generate()
        expires_at = (
            utcnow() + timedelta(days=request.expires_in_days)
            if request.expires_in_days
            else None
        )

        api_key = ApiKey(
            user_id=owner_id,
            name=request.name,
            prefix=prefix,
            key_hash=key_hash,
            scopes=[str(scope) for scope in request.scopes],
            created_at=utcnow(),
            expires_at=expires_at,
        )
        self.db.add(api_key)
        await self.db.commit()

        return CreateApiKeyResponse(
            api_key=ApiKeyDto.model_validate(api_key), key=full_key
        )

    async def list_keys(self, user_id: int | None = None) -> ListApiKeysResponse:
        owner_id = self._resolve_owner_id(user_id)
        keys = await ApiKey.list_for_user(self.db, owner_id)
        return ListApiKeysResponse(data=[ApiKeyDto.model_validate(key) for key in keys])

    async def revoke(self, key_id: int) -> ApiKeyDto:
        """Soft delete: the row stays so `last_used_at` remains readable after
        the fact, which is the point of having per-client credentials."""
        self.principal.require_interactive()

        api_key = await ApiKey.get_by_id(self.db, key_id)
        if api_key is None:
            raise AuthErrors.api_key_not_found()
        # 404 rather than 403: whose key this is isn't the caller's business.
        if api_key.user_id != self.principal.user_id and not self.principal.has_scope(
            Scopes.USERS_ADMIN
        ):
            raise AuthErrors.api_key_not_found()

        if api_key.revoked_at is None:
            api_key.revoked_at = utcnow()
            await self.db.commit()

        return ApiKeyDto.model_validate(api_key)

    @staticmethod
    def get_with_deps(
        db: Annotated[AsyncSession, Depends(DatabaseService.get_async_db_session)],
        principal: Annotated[Principal, Depends(AuthService.get_principal)],
    ) -> ApiKeyService:
        return ApiKeyService(db, principal)
