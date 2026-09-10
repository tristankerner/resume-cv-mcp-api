from typing import Annotated

from fastapi import APIRouter, Depends, Response

from services.auth.auth_service import AuthService
from services.auth.dtos.user import UserDto
from services.user.dtos import CreateUserRequest, CreateUserResponse
from services.user.dtos.change_password import ChangePasswordRequest
from services.user.dtos.list_users import ListUsersResponse
from services.user.dtos.reset_password import AdminResetPasswordRequest
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
        # Registered before the /{user_id} routes for readability; there is no
        # collision since every one of those carries a path segment after it.
        self.router.get("/users")(self.list_users)
        self.router.post("/users")(self.create_user)
        self.router.patch("/users/{user_id}", status_code=204)(self.update_user)
        self.router.post("/users/me/password", status_code=204)(
            self.change_own_password
        )
        # Must stay registered after /users/me/password, or "me" is parsed as
        # this route's int user_id and 422s.
        self.router.post("/users/{user_id}/password", status_code=204)(
            self.reset_password
        )
        self.router.delete("/users/{user_id}/lock", status_code=204)(self.unlock_user)
        self.router.delete("/users/{user_id}/mfa", status_code=204)(self.reset_mfa)
        self.router.delete("/users/{user_id}/passkeys", status_code=204)(
            self.reset_passkeys
        )

    async def read_users_me(self, user_service: UserServiceDep) -> UserDto:
        return await user_service.get_current_user()

    async def list_users(self, user_service: UserServiceDep) -> ListUsersResponse:
        """Every user. Requires users:admin; read-only, so an API key holding
        that scope may call it too — see UserService.list_users."""
        return await user_service.list_users()

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

    async def reset_password(
        self,
        user_id: int,
        request: AdminResetPasswordRequest,
        user_service: UserServiceDep,
    ) -> Response:
        """Admin password reset, bypassing the current one. Requires
        users:admin and an interactive login — see UserService.reset_password.
        """
        await user_service.reset_password(user_id, request)
        return Response(status_code=204)

    async def unlock_user(self, user_id: int, user_service: UserServiceDep) -> Response:
        """End a login lockout. Requires users:admin.

        Reachable with an API key, which is the point: a lockout only blocks
        the password path, so an admin locked out of /token can still unlock
        themselves with a key they already hold. When there is no such key,
        `python -m admin_cli unlock` does the same thing against the database.
        """
        await user_service.unlock_user(user_id)
        return Response(status_code=204)

    async def reset_mfa(self, user_id: int, user_service: UserServiceDep) -> Response:
        """Strip every MFA method from an account. Requires users:admin and
        an interactive login.

        Unlike unlock_user, not reachable with an API key: a lock has to be
        clearable that way so a locked-out admin can still act, but MFA has
        no such constraint, and an admin key that can strip second factors
        would make MFA optional service-wide for whoever steals that key.
        `python -m admin_cli reset-mfa` is the break-glass path when there is
        no interactive login to be had.
        """
        await user_service.reset_mfa(user_id)
        return Response(status_code=204)

    async def reset_passkeys(
        self, user_id: int, user_service: UserServiceDep
    ) -> Response:
        """Strip every passkey from an account. Requires users:admin and an
        interactive login — see UserService.reset_passkeys.
        """
        await user_service.reset_passkeys(user_id)
        return Response(status_code=204)
