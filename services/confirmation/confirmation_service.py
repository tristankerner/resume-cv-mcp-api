"""The two-phase confirmation gate every MCP write tool pair shares: preview
is the default, commit requires a token the server issued. See
`persistence.pending_write.PendingWrite`'s docstring for the invariant this
exists to enforce — `redeem` returns exactly the payload `issue` was given,
never anything the caller of `redeem` supplies itself.

An MCP server cannot make a client ask the user anything. A tool docstring
saying "confirm first" is advice, and a model working under pressure to
finish a task will eventually skip it — and nothing detects that it did. This
is the part of the design that is not advice: a model that never calls
`issue` never receives a token, and `redeem` has nothing to accept.
"""

import secrets
from typing import Any, ClassVar, NamedTuple

from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.base import Clock
from persistence.pending_write import PendingWrite
from services.auth.api_keys import ApiKeyToken


class PreviewResult(NamedTuple):
    preview: dict[str, Any]
    confirm_token: str
    expires_in: int


class ConfirmationService:
    TOKEN_BYTES: ClassVar[int] = 32

    def __init__(self, db: AsyncSession, user_id: int):
        self.db = db
        self.user_id = user_id

    @classmethod
    def _generate_token(cls) -> str:
        return secrets.token_urlsafe(cls.TOKEN_BYTES)

    async def issue(
        self, tool_name: str, payload: dict[str, Any], preview: dict[str, Any]
    ) -> PreviewResult:
        """Persist `payload` under a fresh opaque token and return it
        plaintext exactly once, alongside `preview`. Nothing about `payload`
        is trusted back from the caller at redemption — see the module
        docstring."""
        raw_token = self._generate_token()
        record = PendingWrite(
            token_hash=ApiKeyToken.hash_secret(raw_token),
            user_id=self.user_id,
            tool_name=tool_name,
            payload=payload,
            preview=preview,
            expires_at=Clock.utcnow() + PendingWrite.TTL,
        )
        self.db.add(record)
        await self.db.commit()
        return PreviewResult(
            preview=preview,
            confirm_token=raw_token,
            expires_in=int(PendingWrite.TTL.total_seconds()),
        )

    async def redeem(self, token: str, tool_name: str) -> dict[str, Any]:
        """The frozen payload `issue` was given, or a refusal distinct per
        case: unknown token, expired, already consumed, issued for a
        different tool, issued to a different user. Distinct messages
        matter — the model has to be able to tell "you already did this"
        from "that expired, ask again" and behave differently.

        The row is locked before any of these checks (see
        `PendingWrite.lock_by_hash`), so two concurrent redemptions of the
        same token cannot both succeed. Marks `consumed_at` in the same
        transaction as the commit below; the caller is responsible for
        performing the authorized write in the same database session before
        anything about this transaction is assumed durable by a client.
        """
        record = await PendingWrite.lock_by_hash(
            self.db, ApiKeyToken.hash_secret(token)
        )
        # A token issued to another user is refused the same way an unknown
        # token is — existence is not disclosed, the same reasoning
        # DocumentService applies to a document owned by someone else.
        if record is None or record.user_id != self.user_id:
            raise ToolError("No pending write matches that confirmation token.")
        if record.tool_name != tool_name:
            raise ToolError(
                f"That confirmation token was issued for {record.tool_name!r}, "
                f"not for {tool_name!r}. Call {tool_name}'s preview tool first."
            )
        if record.consumed_at is not None:
            raise ToolError(
                "That confirmation token has already been used. Nothing was "
                "written this time — call the preview tool again if you "
                "meant to make another change."
            )
        if record.expires_at <= Clock.utcnow():
            raise ToolError(
                "That confirmation token has expired. Call the preview tool "
                "again to get a new one."
            )

        record.consumed_at = Clock.utcnow()
        await self.db.commit()
        return record.payload
