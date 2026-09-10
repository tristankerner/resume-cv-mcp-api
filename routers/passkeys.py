from typing import Annotated

from fastapi import APIRouter, Depends, Response

from services.auth.passkeys.dtos.passkey import (
    PasskeyListResponse,
    PasskeyPasswordConfirmRequest,
    PasskeyRegistrationOptionsRequest,
    PasskeyRegistrationOptionsResponse,
    PasskeyRegistrationRequest,
    PasskeyRenameRequest,
    PasskeyStatus,
)
from services.auth.passkeys.passkey_service import PasskeyService


class PasskeysRouter:
    PasskeyServiceDep = Annotated[PasskeyService, Depends(PasskeyService.get_with_deps)]

    def __init__(self) -> None:
        self.router = APIRouter(prefix="/users/me/passkeys", tags=["passkeys"])
        self._register()

    def _register(self) -> None:
        self.router.get("")(self.list_passkeys)
        self.router.post("/options")(self.registration_options)
        self.router.post("")(self.register)
        self.router.patch("/{credential_id}")(self.rename)
        # DELETE with a request body is unusual but legal, and reads better
        # than `POST .../remove` for what is really a delete — see
        # routers/mfa.py's `remove`.
        self.router.delete("/{credential_id}", status_code=204)(self.remove)

    async def list_passkeys(
        self, passkey_service: PasskeyServiceDep
    ) -> PasskeyListResponse:
        return await passkey_service.list()

    async def registration_options(
        self,
        request: PasskeyRegistrationOptionsRequest,
        passkey_service: PasskeyServiceDep,
    ) -> PasskeyRegistrationOptionsResponse:
        return await passkey_service.registration_options(request)

    async def register(
        self, request: PasskeyRegistrationRequest, passkey_service: PasskeyServiceDep
    ) -> PasskeyStatus:
        return await passkey_service.register(request)

    async def rename(
        self,
        credential_id: int,
        request: PasskeyRenameRequest,
        passkey_service: PasskeyServiceDep,
    ) -> PasskeyStatus:
        return await passkey_service.rename(credential_id, request)

    async def remove(
        self,
        credential_id: int,
        request: PasskeyPasswordConfirmRequest,
        passkey_service: PasskeyServiceDep,
    ) -> Response:
        await passkey_service.remove(credential_id, request)
        return Response(status_code=204)
