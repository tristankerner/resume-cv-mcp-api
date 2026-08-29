"""Create the first admin user from the command line.

Application startup does this automatically when BOOTSTRAP_ADMIN_USERNAME and
BOOTSTRAP_ADMIN_PASSWORD are set, which is the path a container takes. This is
the interactive equivalent for a local database, and it prompts for whatever
the environment does not already supply.

    python -m bootstrap_admin
"""

import asyncio
import getpass
import sys

from persistence.user import User
from services.auth.roles import Roles
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService
from services.user.bootstrap import AdminBootstrapper, BootstrapError, BootstrapOutcome


class BootstrapAdminCommand:
    async def run(self) -> int:
        settings = ConfigService.get_without_deps().settings
        configured_password = settings.bootstrap_admin_password

        username = settings.bootstrap_admin_username
        password = (
            configured_password.get_secret_value() if configured_password else None
        )

        # Checked before prompting so a claimed database doesn't ask for a
        # credential it is only going to throw away. ensure_admin checks
        # again, which is what actually settles the race.
        async with DatabaseService.session() as db:
            if await User.any_with_role(db, Roles.ADMIN):
                print("An admin already exists; nothing to do.")
                return 0

        if not username:
            username = input("Admin username: ").strip()
        if not password:
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Retype password: "):
                print("Passwords do not match.", file=sys.stderr)
                return 1

        async with DatabaseService.session() as db:
            try:
                outcome, created = await AdminBootstrapper(db).ensure_admin(
                    username, password, settings.bootstrap_admin_email
                )
            except BootstrapError as error:
                print(error, file=sys.stderr)
                return 1

        match outcome:
            case BootstrapOutcome.CREATED:
                print(f"Created admin {created!r}.")
            case BootstrapOutcome.ALREADY_CLAIMED:
                print("An admin already exists; nothing to do.")
            case BootstrapOutcome.NOT_CONFIGURED:
                print("A username and password are required.", file=sys.stderr)
                return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(BootstrapAdminCommand().run()))
