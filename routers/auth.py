from typing import Annotated

from fastapi import APIRouter, Depends, Form, status
from fastapi.security import OAuth2PasswordRequestForm

from services.auth.dtos.mfa import MfaRequiredResponse
from services.auth.login_service import LoginService
from services.auth.token_data import Token


class AuthRouter:
    LoginServiceDep = Annotated[LoginService, Depends(LoginService.get_with_deps)]
    FormDep = Annotated[OAuth2PasswordRequestForm, Depends()]

    def __init__(self) -> None:
        self.router = APIRouter()
        self._register()

    def _register(self) -> None:
        self.router.post("/token", response_model=None)(self.login_for_access_token)
        self.router.post("/token/mfa")(self.complete_mfa)
        self.router.post("/token/refresh")(self.refresh_token)
        self.router.post("/token/logout", status_code=status.HTTP_204_NO_CONTENT)(
            self.logout
        )

    async def login_for_access_token(
        self, login_service: LoginServiceDep, form_data: FormDep
    ) -> Token | MfaRequiredResponse:
        return await login_service.login(form_data.username, form_data.password)

    async def complete_mfa(
        self,
        login_service: LoginServiceDep,
        mfa_token: Annotated[str, Form()],
        code: Annotated[str, Form()],
    ) -> Token:
        return await login_service.complete_mfa(mfa_token, code)

    async def refresh_token(
        self,
        login_service: LoginServiceDep,
        refresh_token: Annotated[str, Form()],
    ) -> Token:
        return await login_service.refresh(refresh_token)

    async def logout(
        self,
        login_service: LoginServiceDep,
        refresh_token: Annotated[str, Form()],
    ) -> None:
        await login_service.logout(refresh_token)
