"""Strip every MFA method from an account, from the command line.

The break-glass path for a lost authenticator: `DELETE /users/{id}/mfa` does
the same thing over the API for an admin who holds a key and an interactive
login — but MFA management deliberately refuses an API key (see
UserService.reset_mfa), so an admin with no interactive session and nothing
but a key has no way in through the API at all. This talks to the database
directly and answers to nobody, so it needs whatever DATABASE_URL the
deployment uses.

It never decrypts a secret. It deletes rows and counts them; it has no
reason to read `mfa_credentials.secret` and must not start reading one. That
is what makes it the recovery path when `MFA_ENCRYPTION_KEYS` is wrong or
lost — a break-glass tool that needs the key it is recovering from is not
one.

Backup codes survive a lost key on their own (they are hashed, not
encrypted), so a user holding one usually needs nothing from this tool at
all — it exists for the case where the authenticator and every backup code
are both gone. A user who has lost their authenticator but is not otherwise
locked out needs nothing else; one who is *also* locked out needs
`python -m unlock_user` as well.

    python -m reset_mfa alice
    python -m reset_mfa --list
"""

import argparse
import asyncio
import sys

from sqlalchemy import select

from persistence.mfa_credential import MfaCredential
from persistence.user import User
from services.database.database_service import DatabaseService


class ResetMfaCommand:
    @staticmethod
    def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
        parser = argparse.ArgumentParser(
            prog="python -m reset_mfa",
            description="Remove every MFA method from an account.",
        )
        parser.add_argument("username", nargs="?", help="the account to reset")
        parser.add_argument(
            "--list",
            action="store_true",
            help="show every account that has an MFA method, and change nothing",
        )
        return parser.parse_args(argv)

    @staticmethod
    async def _list_enrolled() -> int:
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
    async def _reset_user(username: str) -> int:
        async with DatabaseService.session() as db:
            user = await User.get_user_by_username(db, username)
            if user is None:
                print(f"No user named {username!r}.", file=sys.stderr)
                return 1

            removed = await MfaCredential.delete_all_for_user(db, user.id)
            await db.commit()

        if removed:
            print(f"Removed {removed} MFA method(s) from {username!r}.")
        else:
            print(f"{username!r} has no MFA methods; nothing to do.")
        return 0

    async def run(self, argv: list[str] | None = None) -> int:
        args = self._parse_args(argv)

        if args.list:
            return await self._list_enrolled()
        if args.username:
            return await self._reset_user(args.username)

        print("Give a username or --list. See --help.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(ResetMfaCommand().run()))
