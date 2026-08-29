from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm

from services.auth.auth_service import AuthService
from services.auth.exceptions import AuthErrors
from services.auth.token_data import Token
from services.config.config_service import ConfigService


class AuthRouter:
    AuthServiceDep = Annotated[AuthService, Depends(AuthService.get_with_deps)]
    ConfigServiceDep = Annotated[ConfigService, Depends(ConfigService.get_with_deps)]
    FormDep = Annotated[OAuth2PasswordRequestForm, Depends()]

    def __init__(self) -> None:
        self.router = APIRouter()
        self._register()

    def _register(self) -> None:
        self.router.post("/token")(self.login_for_access_token)

    async def login_for_access_token(
        self,
        auth_service: AuthServiceDep,
        config_service: ConfigServiceDep,
        form_data: FormDep,
    ) -> Token:
        user = await auth_service.authenticate_user(
            form_data.username, form_data.password
        )
        if not user:
            raise AuthErrors.credentials()
        access_token_expires = timedelta(
            minutes=config_service.settings.auth_access_token_expire_minutes
        )
        access_token = auth_service.create_access_token(
            data={"sub": str(user.id)}, expires_delta=access_token_expires
        )
        return Token(access_token=access_token, token_type="bearer")  # nosec B106


router = AuthRouter().router
