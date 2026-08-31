from typing import Annotated

from fastapi import APIRouter, Depends, Response

from services.auth.mfa.dtos.mfa import (
    ActivateTotpRequest,
    BackupCodesResponse,
    EnrollTotpRequest,
    EnrollTotpResponse,
    MfaStatusResponse,
    PasswordConfirmRequest,
)
from services.auth.mfa.mfa_service import MfaService


class MfaRouter:
    MfaServiceDep = Annotated[MfaService, Depends(MfaService.get_with_deps)]

    def __init__(self) -> None:
        self.router = APIRouter(prefix="/users/me/mfa", tags=["mfa"])
        self._register()

    def _register(self) -> None:
        self.router.get("")(self.status)
        self.router.post("/totp")(self.enroll_totp)
        self.router.post("/totp/{credential_id}/activate", status_code=204)(
            self.activate_totp
        )
        self.router.post("/backup-codes")(self.regenerate_backup_codes)
        # DELETE with a request body is unusual but legal, and FastAPI
        # supports it. `POST .../remove` reads worse for what is really a
        # delete, and the browser client's `request()` helper already
        # passes a body independently of method.
        self.router.delete("/{credential_id}", status_code=204)(self.remove)

    async def status(self, mfa_service: MfaServiceDep) -> MfaStatusResponse:
        return await mfa_service.status()

    async def enroll_totp(
        self, request: EnrollTotpRequest, mfa_service: MfaServiceDep
    ) -> EnrollTotpResponse:
        return await mfa_service.enroll_totp(request)

    async def activate_totp(
        self,
        credential_id: int,
        request: ActivateTotpRequest,
        mfa_service: MfaServiceDep,
    ) -> Response:
        await mfa_service.activate_totp(credential_id, request)
        return Response(status_code=204)

    async def regenerate_backup_codes(
        self, request: PasswordConfirmRequest, mfa_service: MfaServiceDep
    ) -> BackupCodesResponse:
        return await mfa_service.regenerate_backup_codes(request)

    async def remove(
        self,
        credential_id: int,
        request: PasswordConfirmRequest,
        mfa_service: MfaServiceDep,
    ) -> Response:
        await mfa_service.remove(credential_id, request)
        return Response(status_code=204)


router = MfaRouter().router
