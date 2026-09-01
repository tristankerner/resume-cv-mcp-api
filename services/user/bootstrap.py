"""Creating the first admin on a database that has none.

The initial migration deliberately seeds no users, so a fresh deployment needs
some way to get its first credential. This module is that way, and it is shared
by two callers: application startup (env-driven, for containers) and the
`bootstrap_admin` CLI (interactive, for local use).

The operation is idempotent by design — it does nothing once any admin exists —
so it is safe to leave wired into startup and safe to run twice.
"""

from enum import StrEnum, auto

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.user import User
from services.auth.auth_service import AuthService
from services.auth.roles import Roles
from services.user.document_seeder import DocumentSeeder
from services.user.dtos import CreateUserRequest


class BootstrapOutcome(StrEnum):
    CREATED = auto()
    ALREADY_CLAIMED = auto()  # an admin exists; the request was a no-op
    NOT_CONFIGURED = auto()  # no admin exists and no credentials were supplied


class BootstrapError(Exception):
    """Configuration that cannot produce an admin.

    Raised rather than warned so a misconfigured container fails loudly at
    startup instead of coming up with no way in.
    """


class AdminBootstrapper:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def ensure_admin(
        self,
        username: str | None,
        password: str | None,
        email: str | None = None,
    ) -> tuple[BootstrapOutcome, str | None]:
        """Create an admin if the database has none. Returns what it decided to do."""
        if await User.any_with_role(self.db, Roles.ADMIN):
            return BootstrapOutcome.ALREADY_CLAIMED, None

        username = (username or "").strip()
        if not username or not password:
            return BootstrapOutcome.NOT_CONFIGURED, None

        # Validate through the same DTO the public registration endpoint uses,
        # so the bootstrap cannot create a user the API's own rules would
        # reject.
        try:
            request = CreateUserRequest(
                username=username, password=password, email=email
            )
        except ValidationError as error:
            raise BootstrapError(
                "Bootstrap admin rejected: "
                + "; ".join(issue["msg"] for issue in error.errors())
            ) from error

        existing = await User.get_user_by_username(self.db, request.username)
        if existing is not None:
            # Promoting an existing account by setting an environment variable
            # would be a surprising way to grant admin. Make the operator decide.
            raise BootstrapError(
                f"User {request.username!r} already exists but is not an admin. "
                "Choose a different bootstrap username, or grant the role directly."
            )

        new_admin = User(
            username=request.username,
            email=request.email,
            password=AuthService.get_password_hash(request.password.get_secret_value()),
            roles=[Roles.ADMIN.value],
        )
        self.db.add(new_admin)
        try:
            # Flushed rather than committed: this is where the race with
            # another instance claiming the same fresh database would surface
            # as an IntegrityError, and it has to happen before the seeded
            # documents can reference the new admin's id.
            await self.db.flush()
        except IntegrityError:
            # Two instances racing to claim the same fresh database. The other
            # one won; there is an admin now either way.
            await self.db.rollback()
            return BootstrapOutcome.ALREADY_CLAIMED, None

        await DocumentSeeder().seed(self.db, new_admin.id)
        await self.db.commit()

        return BootstrapOutcome.CREATED, request.username
