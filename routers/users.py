from typing import Annotated

from fastapi import APIRouter, Depends, Response

from services.auth.auth_service import AuthService
from services.auth.dtos.user import UserDto
from services.user.dtos import CreateUserRequest, CreateUserResponse
from services.user.dtos.change_password import ChangePasswordRequest
from services.user.dtos.update_user import UpdateUserRequest
from services.user.user_service import UserService


class UsersRouter:
    UserServiceDep = Annotated[UserService, Depends(UserService.get_with_deps)]
    AuthServiceDep = Annotated[AuthService, Depends(AuthService.get_with_deps)]

    def __init__(self) -> None:
        self.router = APIRouter()
        self._register()

    def _register(self) -> None:
        self.router.get("/users/me")(self.read_users_me)
        self.router.post("/users")(self.create_user)
        self.router.patch("/users/{user_id}", status_code=204)(self.update_user)
        self.router.post("/users/me/password", status_code=204)(
            self.change_own_password
        )
        self.router.delete("/users/{user_id}/lock", status_code=204)(self.unlock_user)

    async def read_users_me(self, user_service: UserServiceDep) -> UserDto:
        return await user_service.get_current_user()

    async def create_user(
        self, request: CreateUserRequest, user_service: UserServiceDep
    ) -> CreateUserResponse:
        result = await user_service.register_user(request)
        return result

    async def update_user(
        self, user_id: int, request: UpdateUserRequest, user_service: UserServiceDep
    ) -> Response:
        request.user_id = user_id
        await user_service.update_user(request)
        return Response(status_code=204)

    async def change_own_password(
        self,
        request: ChangePasswordRequest,
        user_service: UserServiceDep,
        auth_service: AuthServiceDep,
    ) -> Response:
        """The logged-in user's own password change. Requires an interactive
        login (a password JWT) and the current password — see UserService."""
        await user_service.change_own_password(auth_service, request)
        return Response(status_code=204)

    async def unlock_user(self, user_id: int, user_service: UserServiceDep) -> Response:
        """End a login lockout. Requires users:admin.

        Reachable with an API key, which is the point: a lockout only blocks
        the password path, so an admin locked out of /token can still unlock
        themselves with a key they already hold. When there is no such key,
        `python -m unlock_user` does the same thing against the database.
        """
        await user_service.unlock_user(user_id)
        return Response(status_code=204)


router = UsersRouter().router
