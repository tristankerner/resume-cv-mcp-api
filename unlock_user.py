"""Clear a login lockout from the command line.

The break-glass path for the throttle in services/auth/lockout.py. `DELETE
/users/{id}/lock` is the ordinary way to do this and works over an API key
even while the password login is locked — but an admin who holds no key and
whose only account is permanently locked has no way in through the API at all,
and this is what that situation is for. It talks to the database directly and
answers to nobody, so it needs whatever DATABASE_URL the deployment uses.

    python -m unlock_user alice
    python -m unlock_user --address 203.0.113.7
    python -m unlock_user --list
"""

import argparse
import asyncio
import sys
from typing import cast

from sqlalchemy import CursorResult, delete, select

from persistence.auth_failure import AuthFailure
from persistence.base import utcnow
from persistence.user import User
from services.auth.lockout import AccountLock
from services.database.database_service import DatabaseService


class UnlockCommand:
    @staticmethod
    def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
        parser = argparse.ArgumentParser(
            prog="python -m unlock_user",
            description="Clear a login lockout on an account or a calling address.",
        )
        parser.add_argument("username", nargs="?", help="the account to unlock")
        parser.add_argument(
            "--address",
            help="clear a banned calling address instead of an account",
        )
        parser.add_argument(
            "--list",
            action="store_true",
            help="show what is currently locked, and change nothing",
        )
        return parser.parse_args(argv)

    @staticmethod
    async def _list_locked() -> int:
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

    async def run(self, argv: list[str] | None = None) -> int:
        args = self._parse_args(argv)

        if args.list:
            return await self._list_locked()
        if args.address:
            return await self._unlock_address(args.address)
        if args.username:
            return await self._unlock_user(args.username)

        print(
            "Give a username, --address, or --list. See --help.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(UnlockCommand().run()))
