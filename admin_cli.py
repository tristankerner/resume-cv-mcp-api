"""Last-resort account recovery, direct against the database.

Each subcommand below has an equivalent over the API, and in the browser
client if one is in front of this deployment: `DELETE /users/{id}/lock`,
`POST /users/{id}/password`, `DELETE /users/{id}/mfa`, and application
startup's own admin bootstrap. Reach for this script only when there is no
working admin login to use one of those with — it talks to the database
directly, with whatever DATABASE_URL the deployment uses, and needs no HTTP
credential at all.

`unlock --address` is the one thing here with no API equivalent: a banned
calling address is not tied to an account, and there is no route that clears
one.

    python -m admin_cli unlock <username> [--address ADDR] [--list]
    python -m admin_cli reset-password <username>
    python -m admin_cli reset-mfa <username> [--list]
    python -m admin_cli bootstrap-admin
"""

import argparse
import asyncio
import getpass
import sys
from typing import cast

from pydantic import SecretStr
from sqlalchemy import CursorResult, delete, select

from persistence.auth_failure import AuthFailure
from persistence.base import utcnow
from persistence.mfa_credential import MfaCredential
from persistence.user import User
from services.auth.auth_service import AuthService
from services.auth.lockout import AccountLock
from services.auth.roles import Roles
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService
from services.user.bootstrap import AdminBootstrapper, BootstrapError, BootstrapOutcome
from services.user.password_policy import PasswordPolicy


class AdminCli:
    @staticmethod
    def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
        parser = argparse.ArgumentParser(
            prog="python -m admin_cli",
            description=(
                "Last-resort account recovery, direct against the database. "
                "Everything here is also reachable over the API, or the "
                "browser client, given a working admin login."
            ),
        )
        subparsers = parser.add_subparsers(dest="command", required=True)

        unlock = subparsers.add_parser(
            "unlock",
            help="clear a login lockout on an account or a calling address",
        )
        unlock.add_argument("username", nargs="?", help="the account to unlock")
        unlock.add_argument(
            "--address",
            help="clear a banned calling address instead of an account",
        )
        unlock.add_argument(
            "--list",
            action="store_true",
            help="show what is currently locked, and change nothing",
        )

        reset_password = subparsers.add_parser(
            "reset-password",
            help="set a new password for an account, bypassing the current one",
        )
        reset_password.add_argument("username", help="the account to reset")

        reset_mfa = subparsers.add_parser(
            "reset-mfa", help="remove every MFA method from an account"
        )
        reset_mfa.add_argument("username", nargs="?", help="the account to reset")
        reset_mfa.add_argument(
            "--list",
            action="store_true",
            help="show every account that has an MFA method, and change nothing",
        )

        subparsers.add_parser(
            "bootstrap-admin", help="create the first admin on an empty database"
        )

        return parser.parse_args(argv)

    # --- unlock -------------------------------------------------------

    @staticmethod
    async def _unlock_list() -> int:
        now = utcnow()
        async with DatabaseService.session() as db:
            users = (
                (
                    await db.execute(
                        select(User).where(
                            User.locked_permanently_at.is_not(None)
                            | (User.locked_until > now)
                        )
                    )
                )
                .scalars()
                .all()
            )
            addresses = (
                (
                    await db.execute(
                        select(AuthFailure).where(AuthFailure.banned_until > now)
                    )
                )
                .scalars()
                .all()
            )

        if not users and not addresses:
            print("Nothing is locked.")
            return 0

        for user in users:
            if user.locked_permanently_at is not None:
                print(
                    f"{user.username}: locked permanently at "
                    f"{user.locked_permanently_at}"
                )
            else:
                print(f"{user.username}: locked until {user.locked_until}")
        for failure in addresses:
            print(f"{failure.address}: banned until {failure.banned_until}")
        return 0

    @staticmethod
    async def _unlock_address(address: str) -> int:
        async with DatabaseService.session() as db:
            result = await db.execute(
                delete(AuthFailure).where(AuthFailure.address == address)
            )
            await db.commit()

        # Deleting the row rather than clearing the ban leaves the same state
        # `prune` would.
        if not cast(CursorResult, result).rowcount:
            print(f"No failures recorded for {address!r}; nothing to do.")
            return 0
        print(f"Cleared {address!r}.")
        return 0

    @staticmethod
    async def _unlock_user(username: str) -> int:
        async with DatabaseService.session() as db:
            user = await User.get_user_by_username(db, username)
            if user is None:
                print(f"No user named {username!r}.", file=sys.stderr)
                return 1

            was_locked = (
                user.locked_permanently_at is not None
                or user.locked_until is not None
                or user.lock_count > 0
                or user.failed_login_count > 0
            )
            AccountLock.unlock(user)
            await db.commit()

        if was_locked:
            print(f"Unlocked {username!r}.")
        else:
            print(f"{username!r} was not locked; nothing to do.")
        return 0

    async def _run_unlock(self, args: argparse.Namespace) -> int:
        if args.list:
            return await self._unlock_list()
        if args.address:
            return await self._unlock_address(args.address)
        if args.username:
            return await self._unlock_user(args.username)

        print(
            "Give a username, --address, or --list. See --help.",
            file=sys.stderr,
        )
        return 1

    # --- reset-password -------------------------------------------------

    @staticmethod
    def _prompt_new_password() -> str:
        while True:
            password = getpass.getpass("New password: ")
            if password != getpass.getpass("Retype new password: "):
                print("Passwords did not match; try again.", file=sys.stderr)
                continue
            try:
                PasswordPolicy.validate(SecretStr(password))
            except ValueError as error:
                print(error, file=sys.stderr)
                continue
            return password

    async def _run_reset_password(self, args: argparse.Namespace) -> int:
        # Two short sessions rather than one held open across the prompt:
        # `getpass` blocks for as long as the operator takes to type, and a
        # connection idle in a transaction for minutes is one the server may
        # close first (Neon does). The first session only fails fast on a
        # name that does not exist, before asking for anything.
        async with DatabaseService.session() as db:
            if await User.get_user_by_username(db, args.username) is None:
                print(f"No user named {args.username!r}.", file=sys.stderr)
                return 1

        password = self._prompt_new_password()

        async with DatabaseService.session() as db:
            # Re-read rather than reusing the row above: that session is
            # closed, and the account may have been removed while the prompt
            # was open.
            user = await User.get_user_by_username(db, args.username)
            if user is None:
                print(f"No user named {args.username!r}.", file=sys.stderr)
                return 1
            user.password = AuthService.get_password_hash(password)
            await db.commit()

        print(f"Password reset for {args.username!r}.")
        return 0

    # --- reset-mfa -------------------------------------------------

    @staticmethod
    async def _reset_mfa_list() -> int:
        async with DatabaseService.session() as db:
            users = (await db.execute(select(User))).scalars().all()
            printed = False
            for user in users:
                credentials = await MfaCredential.list_for_user(db, user.id)
                if not credentials:
                    continue
                kinds = ", ".join(
                    sorted({credential.kind for credential in credentials})
                )
                print(f"{user.username}: {kinds}")
                printed = True

        if not printed:
            print("No accounts have an MFA method.")
        return 0

    @staticmethod
    async def _reset_mfa_user(username: str) -> int:
        async with DatabaseService.session() as db:
            user = await User.get_user_by_username(db, username)
            if user is None:
                print(f"No user named {username!r}.", file=sys.stderr)
                return 1

            # Never decrypts a secret — deletes rows and counts them. That is
            # what makes this usable when MFA_ENCRYPTION_KEYS is wrong or
            # lost.
            removed = await MfaCredential.delete_all_for_user(db, user.id)
            await db.commit()

        if removed:
            print(f"Removed {removed} MFA method(s) from {username!r}.")
        else:
            print(f"{username!r} has no MFA methods; nothing to do.")
        return 0

    async def _run_reset_mfa(self, args: argparse.Namespace) -> int:
        if args.list:
            return await self._reset_mfa_list()
        if args.username:
            return await self._reset_mfa_user(args.username)

        print("Give a username or --list. See --help.", file=sys.stderr)
        return 1

    # --- bootstrap-admin -------------------------------------------------

    async def _run_bootstrap_admin(self) -> int:
        settings = ConfigService.get_without_deps().settings
        configured_password = settings.bootstrap_admin_password

        username = settings.bootstrap_admin_username
        password = (
            configured_password.get_secret_value() if configured_password else None
        )

        # Checked before prompting so a claimed database doesn't ask for a
        # credential it will throw away. ensure_admin checks again, which is
        # what settles the race.
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

    async def run(self, argv: list[str] | None = None) -> int:
        args = self._parse_args(argv)
        if args.command == "unlock":
            return await self._run_unlock(args)
        if args.command == "reset-password":
            return await self._run_reset_password(args)
        if args.command == "reset-mfa":
            return await self._run_reset_mfa(args)
        return await self._run_bootstrap_admin()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(AdminCli().run()))
