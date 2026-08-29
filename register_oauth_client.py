"""Pre-register an OAuth client from the command line.

The normal way a client gets a client_id here. Anonymous Dynamic Client
Registration is off by default (`OAUTH_REGISTRATION_ENABLED`), because
POST /oauth/register cannot require a credential — a client registers before
any user is involved — and an endpoint like that is an unauthenticated
database write anyone who finds it can repeat. The redirect allowlist bounds
where an authorization code may be *delivered*; it does nothing about how many
rows a stranger may create. Closing the endpoint removes the write path
instead of bounding it.

Registering here rather than over HTTP is what makes that affordable: this
talks to the database directly, so it needs whatever DATABASE_URL the
deployment uses, and it cannot be reached with a stolen token.

Both Claude surfaces accept a client_id issued this way:

  - Claude Code: `claude mcp add --client-id ... --client-secret ...`, with
    `--callback-port` to pin the loopback port so the redirect URI registered
    here matches the one it will actually use.
  - claude.ai, Desktop, mobile, Cowork: Advanced settings on the custom
    connector, which exists for authorization servers that do not offer DCR.

    python -m register_oauth_client --name "Claude" \
        --redirect-uri https://claude.ai/api/mcp/auth_callback

    python -m register_oauth_client --name "Claude Code" \
        --redirect-uri http://127.0.0.1:41703/callback --public

    python -m register_oauth_client --list

The secret is printed once and never again — only its hash is stored.
"""

import argparse
import asyncio
import sys

from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService
from services.oauth.clients import OAuthClientRegistry
from services.oauth.exceptions import OAuthError


class RegisterOAuthClientCommand:
    @staticmethod
    def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
        parser = argparse.ArgumentParser(
            prog="python -m register_oauth_client",
            description="Pre-register an OAuth client for the MCP endpoint.",
        )
        parser.add_argument(
            "--name", help="human-readable name, shown on the consent page"
        )
        parser.add_argument(
            "--redirect-uri",
            action="append",
            dest="redirect_uris",
            metavar="URI",
            help="exact redirect URI; repeat for more than one",
        )
        parser.add_argument(
            "--public",
            action="store_true",
            help=(
                "issue a public client with no secret, relying on PKCE alone. "
                "Use for a client that cannot keep one, such as a native app."
            ),
        )
        parser.add_argument("--scope", help="space-delimited default scopes")
        parser.add_argument(
            "--list",
            action="store_true",
            help="show registered clients, and change nothing",
        )
        return parser.parse_args(argv)

    @staticmethod
    async def _list_clients() -> int:
        from sqlalchemy import select

        from persistence.oauth_client import OAuthClient

        async with DatabaseService.session() as db:
            rows = (await db.execute(select(OAuthClient))).scalars().all()

        if not rows:
            print("No OAuth clients registered.")
            return 0
        for client in rows:
            kind = "public" if client.client_secret_hash is None else "confidential"
            print(f"{client.client_id}  {kind}  {client.client_name}")
            for uri in client.redirect_uris:
                print(f"    {uri}")
        return 0

    @staticmethod
    async def _register(args: argparse.Namespace) -> int:
        settings = ConfigService.get_without_deps().settings

        # Validated against the same allowlist an HTTP registration would
        # face — a URI typed at a terminal is no more trustworthy than one
        # that arrives over the wire, and a typo caught here is a typo not
        # debugged later at authorize time.
        try:
            async with DatabaseService.session() as db:
                client, secret = await OAuthClientRegistry(db).create(
                    redirect_uris=args.redirect_uris,
                    client_name=args.name,
                    token_endpoint_auth_method=(
                        "none" if args.public else "client_secret_post"
                    ),
                    scope=args.scope,
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                    allowed_hosts=settings.oauth_allowed_redirect_hosts,
                )
        except OAuthError as exc:
            print(f"error: {exc.description or exc.error}", file=sys.stderr)
            print(
                "hint: OAUTH_ALLOWED_REDIRECT_HOSTS is "
                f"{sorted(settings.oauth_allowed_redirect_hosts)}",
                file=sys.stderr,
            )
            return 1

        print(f"client_id:     {client.client_id}")
        if secret is None:
            print("client_secret: (none — public client, PKCE only)")
        else:
            print(f"client_secret: {secret}")
            print("\nThe secret is shown once. Only its hash is stored.")
        print("\nredirect_uris:")
        for uri in client.redirect_uris:
            print(f"  {uri}")
        return 0

    async def run(self, argv: list[str] | None = None) -> int:
        args = self._parse_args(argv)
        if args.list:
            return await self._list_clients()
        if not args.redirect_uris:
            print(
                "error: --redirect-uri is required (or use --list)",
                file=sys.stderr,
            )
            return 1
        return await self._register(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(RegisterOAuthClientCommand().run()))
