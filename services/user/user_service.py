from typing import Annotated

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.base import Clock
from persistence.mfa_credential import MfaCredential
from persistence.user import User
from services.auth.auth_service import AuthService
from services.auth.dtos.user import UserDto
from services.auth.lockout import AccountLock
from services.auth.mfa.verifier import MfaVerifier
from services.auth.principal import Principal
from services.auth.scopes import ScopeResolver, Scopes
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService
from services.user.document_seeder import DocumentSeeder
from services.user.dtos.change_password import ChangePasswordRequest
from services.user.dtos.list_users import AdminUserDto, ListUsersResponse
from services.user.dtos.reset_password import AdminResetPasswordRequest
from services.user.dtos.update_user import UpdateUserRequest
from services.user.exceptions import UserErrors

from ..service_interface import ServiceProviderInterface
from .dtos import CreateUserRequest, CreateUserResponse


class UserService(ServiceProviderInterface):
    def __init__(
        self, db: AsyncSession, principal: Principal, config_service: ConfigService
    ):
        self.db: AsyncSession = db
        self.principal: Principal = principal
        self.config_service = config_service

    async def get_current_user(self) -> UserDto:
        user = await User.get_user_by_id(self.db, self.principal.user_id)
        if user is None:
            raise UserErrors.not_found()
        dto = UserDto.model_validate(user)
        dto.scopes = sorted(await ScopeResolver.for_roles(self.db, user.roles))
        dto.mfa_enrolled = await MfaVerifier(
            self.db, self.config_service.settings
        ).is_enrolled(user)
        return dto

    async def list_users(self) -> ListUsersResponse:
        """Every user, with the fields an admin UI needs to render the
        unlock / reset-password / reset-mfa / deactivate actions without a
        call per row. Requires users:admin; read-only, so an API key holding
        that scope may call it too, the same as `unlock_user`.
        """
        self.principal.require_scope(Scopes.USERS_ADMIN)

        now = Clock.utcnow()
        users = (
            (await self.db.execute(select(User).order_by(User.username)))
            .scalars()
            .all()
        )
        # One query for MFA enrolment across every user, rather than
        # MfaVerifier.is_enrolled per row — an N+1 that grows with the
        # account table.
        enrolled_ids = frozenset(
            (
                await self.db.execute(
                    select(MfaCredential.user_id)
                    .where(MfaCredential.activated_at.is_not(None))
                    .distinct()
                )
            )
            .scalars()
            .all()
        )

        data: list[AdminUserDto] = []
        for user in users:
            dto = AdminUserDto.model_validate(user)
            dto.mfa_enrolled = user.id in enrolled_ids
            dto.locked = AccountLock.is_locked(user, now)
            data.append(dto)
        return ListUsersResponse(data=data)

    async def register_user(self, request: CreateUserRequest) -> CreateUserResponse:
        """Create an account. Requires users:admin and an interactive login.

        Interactive for the same reason `reset_password` is, and it has to be
        or that one is decorative: creating an account sets its password, so a
        credential that cannot log in interactively could otherwise mint one
        that can — with `roles: ["admin"]`, a peer of the caller — and walk
        straight past every other `require_interactive` gate in this service.
        """
        self.principal.require_scope(Scopes.USERS_ADMIN)
        self.principal.require_interactive()

        existing_user = await User.get_user_by_username(self.db, request.username)
        if existing_user is not None:
            raise UserErrors.username_taken()

        new_user = User(
            username=request.username,
            email=request.email,
            password=AuthService.get_password_hash(request.password.get_secret_value()),
            first_name=request.first_name,
            last_name=request.last_name,
            roles=[role.value for role in request.roles],
        )
        # Inverted: `disabled=True` means `active=False`. Omitted leaves the
        # column's own default (True) in place.
        if request.disabled is not None:
            new_user.active = not request.disabled
        self.db.add(new_user)
        # Flushed rather than committed, so the seeded documents land in the
        # same transaction as the user row — a user is never created without
        # them. Document.created_by is a foreign key to users.id, so the id
        # has to exist in the database before DocumentSeeder can use it.
        await self.db.flush()
        await DocumentSeeder().seed(self.db, new_user.id)
        await self.db.commit()
        return CreateUserResponse(id=new_user.id)

    async def update_user(self, request: UpdateUserRequest) -> bool:
        if request.user_id is None:
            raise UserErrors.no_user_specified()

        is_admin = self.principal.has_scope(Scopes.USERS_ADMIN)
        if not is_admin and self.principal.user_id != request.user_id:
            raise UserErrors.not_found()

        # An edit touching an account-recovery field needs a password login,
        # not just any credential carrying the scope for it — whether it is a
        # self-edit or an admin acting on someone else.
        #
        # Admin used to be exempt here, on the grounds that admin automation
        # resetting a password is a real use. It is, but the exemption made
        # every other `require_interactive` in this service decorative: an
        # admin API key could set any account's password through this route
        # and then log in interactively as that account. `roles` is included
        # for the same reason — promoting an account the caller can already
        # authenticate as is the same escalation by a different door.
        touches_recovery_fields = (
            request.password is not None
            or request.username is not None
            or request.email is not None
            or request.roles is not None
        )
        if touches_recovery_fields:
            self.principal.require_interactive()

        # Deactivation needs users:admin, on anyone's account including the
        # caller's own. `active` is checked on every credential, so clearing it
        # kills the password login, every live token and every API key at once,
        # and the account cannot authenticate to re-enable itself. Left
        # ungated, a key narrowed to `resume:read` could brick its owner.
        if request.disabled is not None:
            self.principal.require_scope(Scopes.USERS_ADMIN)

        existing_user = await User.get_user_by_id(self.db, request.user_id)
        if existing_user is None:
            raise UserErrors.not_found()

        if request.username is not None and request.username != existing_user.username:
            # Checked up front: letting the unique constraint catch it turns a
            # user error into a 500.
            collision = await User.get_user_by_username(self.db, request.username)
            if collision is not None:
                raise UserErrors.username_taken()
            existing_user.username = request.username

        existing_user.email = (
            request.email if request.email is not None else existing_user.email
        )
        existing_user.password = (
            AuthService.get_password_hash(request.password.get_secret_value())
            if request.password is not None
            else existing_user.password
        )
        existing_user.first_name = (
            request.first_name
            if request.first_name is not None
            else existing_user.first_name
        )
        existing_user.last_name = (
            request.last_name
            if request.last_name is not None
            else existing_user.last_name
        )
        # Inverted: `disabled=True` means `active=False`. Omitted leaves
        # `active` unchanged. Guarded by the users:admin check above, so
        # reaching here means the caller holds it.
        if request.disabled is not None:
            existing_user.active = not request.disabled
        if is_admin and request.roles is not None:
            existing_user.roles = [item.value for item in request.roles]

        await self.db.commit()
        return True

    async def change_own_password(
        self, auth_service: AuthService, request: ChangePasswordRequest
    ) -> None:
        """The logged-in user's own self-service password change.

        Its own route rather than a `current_password` field on
        UpdateUserRequest: that DTO also serves the admin reset path, where
        there is no current password to check. Interactive-only for the same
        reason update_user is — a key that can change the password makes every
        other restriction on that key meaningless.
        """
        self.principal.require_interactive()

        user = await User.get_user_by_id(self.db, self.principal.user_id)
        if user is None:
            raise UserErrors.not_found()

        await auth_service.change_password(
            user,
            request.current_password.get_secret_value(),
            request.new_password.get_secret_value(),
        )

    async def reset_password(
        self, user_id: int, request: AdminResetPasswordRequest
    ) -> None:
        """Set a user's password without knowing the current one. Requires
        users:admin and an interactive login — an admin key that can set any
        password owns every account outright, the same reasoning as
        `reset_mfa`. `python -m admin_cli reset-password` is the break-glass
        path when there is no interactive login to be had.
        """
        self.principal.require_scope(Scopes.USERS_ADMIN)
        self.principal.require_interactive()

        user = await User.get_user_by_id(self.db, user_id)
        if user is None:
            raise UserErrors.not_found()

        user.password = AuthService.get_password_hash(
            request.new_password.get_secret_value()
        )
        await self.db.commit()

    async def unlock_user(self, user_id: int) -> bool:
        """Clear a login lockout, temporary or permanent.

        Its own route rather than a field on the update request, which would
        make lock state settable — an admin should be able to end a lockout,
        not impose one by hand. Not restricted to permanently locked accounts.
        """
        self.principal.require_scope(Scopes.USERS_ADMIN)

        user = await User.get_user_by_id(self.db, user_id)
        if user is None:
            raise UserErrors.not_found()

        AccountLock.unlock(user)
        await self.db.commit()
        return True

    async def reset_mfa(self, user_id: int) -> None:
        """Remove every MFA method from an account. Requires users:admin.

        Unlike `unlock_user`, this also requires an interactive login: a lock
        must stay clearable with an API key so a locked-out admin can still
        act, but an admin key that could strip second factors would make MFA
        optional service-wide for whoever steals it. `python -m admin_cli
        reset-mfa` is the break-glass path when there is no interactive login
        to be had.

        Idempotent: an account with no MFA returns having done nothing.
        """
        self.principal.require_scope(Scopes.USERS_ADMIN)
        self.principal.require_interactive()

        user = await User.get_user_by_id(self.db, user_id)
        if user is None:
            raise UserErrors.not_found()

        await MfaCredential.delete_all_for_user(self.db, user_id)
        await self.db.commit()

    @staticmethod
    def get_with_deps(
        db: Annotated[AsyncSession, Depends(DatabaseService.get_async_db_session)],
        principal: Annotated[Principal, Depends(AuthService.get_principal)],
        config_service: Annotated[ConfigService, Depends(ConfigService.get_with_deps)],
    ) -> UserService:
        return UserService(db, principal, config_service)
