"""Admin registration, listing and deregistration of OAuth clients.

Deliberately not under /oauth/*: that surface is the RFC 6749/7591
authorization server, and its errors are the flat {"error", "error_description"}
shape (see services/oauth/exceptions.py). An admin JSON API with
{"detail": ...} errors and bearer auth does not belong in that namespace;
keeping it separate also leaves POST /oauth/register (anonymous DCR, still
gated by OAUTH_REGISTRATION_ENABLED) unchanged and unconfused.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from services.oauth.client_admin_service import OAuthClientAdminService
from services.oauth.dtos import (
    AdminCreateClientRequest,
    AdminCreateClientResponse,
    ListClientsResponse,
)


class OAuthClientsRouter:
    OAuthClientAdminServiceDep = Annotated[
        OAuthClientAdminService, Depends(OAuthClientAdminService.get_with_deps)
    ]

    def __init__(self) -> None:
        self.router = APIRouter(prefix="/oauth-clients", tags=["oauth-clients"])
        self._register()

    def _register(self) -> None:
        self.router.get("")(self.list_clients)
        self.router.post("")(self.create_client)
        self.router.delete("/{client_id}", status_code=204)(self.delete_client)

    async def list_clients(
        self, service: OAuthClientAdminServiceDep
    ) -> ListClientsResponse:
        """List registered clients. Requires users:admin and an interactive
        login — see OAuthClientAdminService."""
        return await service.list_clients()

    async def create_client(
        self, request: AdminCreateClientRequest, service: OAuthClientAdminServiceDep
    ) -> AdminCreateClientResponse:
        """Register a client. The secret in the response is not recoverable
        afterwards. Requires users:admin and an interactive login."""
        return await service.create(request)

    async def delete_client(
        self, client_id: str, service: OAuthClientAdminServiceDep
    ) -> Response:
        """Deregister a client, invalidating every authorization code and
        refresh token it holds. Requires users:admin and an interactive
        login."""
        await service.delete_client(client_id)
        return Response(status_code=204)
