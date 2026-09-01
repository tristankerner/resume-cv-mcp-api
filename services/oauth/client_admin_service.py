"""Admin registration, listing and deregistration of OAuth clients.

Distinct from services/oauth/oauth_service.py, which is the RFC 6749/7591
authorization server surface with its own flat error shape — this is an
ordinary admin JSON API with {"detail": ...} errors and bearer auth, so it
gets its own router (routers/oauth_clients.py) and its own service rather
than growing OAuthService's scope. Creation still delegates to
OAuthClientRegistry.create so the one existing redirect-allowlist check
applies identically to a client registered here.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.oauth_authorization_code import OAuthAuthorizationCode
from persistence.oauth_client import OAuthClient
from persistence.oauth_refresh_token import OAuthRefreshToken
from services.auth.auth_service import AuthService
from services.auth.principal import Principal
from services.auth.scopes import Scopes
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService
from services.oauth.clients import OAuthClientRegistry
from services.oauth.dtos import (
    AdminClientDto,
    AdminCreateClientRequest,
    AdminCreateClientResponse,
    ListClientsResponse,
)
from services.oauth.exceptions import OAuthClientAdminErrors, OAuthError
from services.service_interface import ServiceProviderInterface


class OAuthClientAdminService(ServiceProviderInterface):
    def __init__(
        self, db: AsyncSession, principal: Principal, config_service: ConfigService
    ):
        self.db = db
        self.principal = principal
        self.config_service = config_service

    def _require_admin(self) -> None:
        """users:admin and an interactive login: an API key that can mint an
        OAuth client can mint itself a fresh path into the service, the same
        reasoning as UserService.reset_mfa."""
        self.principal.require_scope(Scopes.USERS_ADMIN)
        self.principal.require_interactive()

    @staticmethod
    def _to_dto(client: OAuthClient) -> AdminClientDto:
        dto = AdminClientDto.model_validate(client)
        dto.confidential = client.client_secret_hash is not None
        return dto

    async def create(
        self, request: AdminCreateClientRequest
    ) -> AdminCreateClientResponse:
        self._require_admin()

        try:
            client, secret = await OAuthClientRegistry(self.db).create(
                redirect_uris=request.redirect_uris,
                client_name=request.client_name,
                token_endpoint_auth_method=(
                    "none" if request.public else "client_secret_post"
                ),
                scope=request.scope,
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                allowed_hosts=self.config_service.settings.oauth_allowed_redirect_hosts,
            )
        except OAuthError as exc:
            # Translated here, the one place, so this surface stays on the
            # app's {"detail": ...} shape rather than leaking the OAuth RFC's
            # flat error shape onto a route outside that surface.
            raise OAuthClientAdminErrors.invalid_request(
                exc.description or exc.error
            ) from exc

        return AdminCreateClientResponse(
            client=self._to_dto(client), client_secret=secret
        )

    async def list_clients(self) -> ListClientsResponse:
        self._require_admin()
        clients = await OAuthClient.list_all(self.db)
        return ListClientsResponse(data=[self._to_dto(client) for client in clients])

    async def delete_client(self, client_id: str) -> None:
        self._require_admin()

        client = await OAuthClient.get_by_client_id(self.db, client_id)
        if client is None:
            raise OAuthClientAdminErrors.not_found()

        # Deregistering a client must invalidate every grant it holds — that
        # is the point of the operation, not a side effect to avoid.
        # Dependants go first: both columns are foreign keys onto
        # oauth_clients.client_id, so Postgres would otherwise refuse the
        # delete outright, and SQLite (foreign keys off in the test config)
        # would silently leave rows that still exchange.
        await OAuthAuthorizationCode.delete_for_client(self.db, client_id)
        await OAuthRefreshToken.delete_for_client(self.db, client_id)
        await OAuthClient.delete_by_client_id(self.db, client_id)
        await self.db.commit()

    @staticmethod
    def get_with_deps(
        db: Annotated[AsyncSession, Depends(DatabaseService.get_async_db_session)],
        principal: Annotated[Principal, Depends(AuthService.get_principal)],
        config_service: Annotated[ConfigService, Depends(ConfigService.get_with_deps)],
    ) -> OAuthClientAdminService:
        return OAuthClientAdminService(db, principal, config_service)
