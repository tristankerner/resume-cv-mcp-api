"""Set a user's password from the command line, without knowing the old one.

The break-glass path for account recovery: `PATCH /users/{id}` does the same
thing over the API for an admin who holds a key, but an admin locked out with
no key has no way in through the API at all. This talks to the database
directly and answers to nobody, so it needs whatever DATABASE_URL the
deployment uses. It only sets the password — an account that is also locked
out still needs `python -m unlock_user` to let the new password back in.

    python -m reset_password alice
"""

import argparse
import asyncio
import getpass
import sys

from pydantic import SecretStr

from persistence.user import User
from services.auth.auth_service import AuthService
from services.database.database_service import DatabaseService
from services.user.password_policy import PasswordPolicy


class ResetPasswordCommand:
    @staticmethod
    def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
        parser = argparse.ArgumentParser(
            prog="python -m reset_password",
            description="Set a new password for an account, bypassing the current one.",
        )
        parser.add_argument("username", help="the account to reset")
        return parser.parse_args(argv)

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

    async def run(self, argv: list[str] | None = None) -> int:
        args = self._parse_args(argv)

        # Two short sessions rather than one held open across the prompt:
        # `getpass` blocks for as long as the operator takes to type, and a
        # connection idle in a transaction for minutes is one the server may
        # close first (Neon does). The first session only fails fast on a name
        # that does not exist, before asking for anything.
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


if __name__ == "__main__":
    raise SystemExit(asyncio.run(ResetPasswordCommand().run()))
