"""`ConfirmationService` — the two-phase preview/confirm token every MCP write
tool pair shares. See services/confirmation/confirmation_service.py and
persistence/pending_write.py.
"""

from datetime import timedelta

import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy import func, select

from persistence.base import Clock
from persistence.pending_write import PendingWrite
from services.auth.api_keys import ApiKeyToken
from services.confirmation.confirmation_service import ConfirmationService
from services.database.database_service import DatabaseService


async def _row_count() -> int:
    async with DatabaseService.session() as db:
        return (
            await db.execute(select(func.count()).select_from(PendingWrite))
        ).scalar_one()


class TestIssue:
    async def test_issue_returns_the_preview_and_a_token(self, admin):
        async with DatabaseService.session() as db:
            result = await ConfirmationService(db, admin.user_id).issue(
                "create_company", {"name": "Acme"}, {"would_create": "Acme"}
            )
        assert result.preview == {"would_create": "Acme"}
        assert result.confirm_token
        assert result.expires_in == int(PendingWrite.TTL.total_seconds())

    async def test_a_preview_never_committed_writes_nothing(self, admin):
        """Issuing a preview only ever writes the pending_writes bookkeeping
        row — it must never itself perform the write it is previewing.
        Nothing in ConfirmationService touches any other table, so this is
        really asserting `issue` is side-effect-free beyond its own row."""
        async with DatabaseService.session() as db:
            await ConfirmationService(db, admin.user_id).issue(
                "create_company", {"name": "Acme"}, {"would_create": "Acme"}
            )
        assert await _row_count() == 1


class TestRedeem:
    async def _issue(self, user_id: int, tool_name: str = "create_company"):
        async with DatabaseService.session() as db:
            return await ConfirmationService(db, user_id).issue(
                tool_name, {"name": "Acme"}, {"would_create": "Acme"}
            )

    async def test_redeem_returns_the_frozen_payload(self, admin):
        result = await self._issue(admin.user_id)
        async with DatabaseService.session() as db:
            payload = await ConfirmationService(db, admin.user_id).redeem(
                result.confirm_token, "create_company"
            )
        assert payload == {"name": "Acme"}

    async def test_redeem_ignores_the_confirm_calls_own_arguments(self, admin):
        """The committed payload is always the preview's payload, never
        anything a confirm_* call might pass alongside the token — there is
        no argument to pass here at all, which is the point: `redeem` takes
        the token and nothing else."""
        result = await self._issue(admin.user_id)
        async with DatabaseService.session() as db:
            payload = await ConfirmationService(db, admin.user_id).redeem(
                result.confirm_token, "create_company"
            )
        assert payload == {"name": "Acme"}
        assert "confirm_token" not in payload

    async def test_commit_with_no_prior_preview_is_refused(self, admin):
        async with DatabaseService.session() as db:
            with pytest.raises(ToolError):
                await ConfirmationService(db, admin.user_id).redeem(
                    "not-a-real-token", "create_company"
                )

    async def test_commit_for_a_different_tool_is_refused(self, admin):
        result = await self._issue(admin.user_id, tool_name="create_company")
        async with DatabaseService.session() as db:
            with pytest.raises(ToolError, match="create_company"):
                await ConfirmationService(db, admin.user_id).redeem(
                    result.confirm_token, "create_contact"
                )

    async def test_commit_twice_is_refused_and_nothing_is_written_the_second_time(
        self, admin
    ):
        result = await self._issue(admin.user_id)
        async with DatabaseService.session() as db:
            first = await ConfirmationService(db, admin.user_id).redeem(
                result.confirm_token, "create_company"
            )
        assert first == {"name": "Acme"}

        assert await _row_count() == 1

        async with DatabaseService.session() as db:
            with pytest.raises(ToolError, match="already"):
                await ConfirmationService(db, admin.user_id).redeem(
                    result.confirm_token, "create_company"
                )

        # Still exactly one row: the second redemption did not consume a
        # fresh row, write a new one, or otherwise change how many rows
        # exist.
        assert await _row_count() == 1

    async def test_commit_after_ttl_is_refused(self, admin):
        result = await self._issue(admin.user_id)
        async with DatabaseService.session() as db:
            record = await PendingWrite.get_by_hash(
                db, ApiKeyToken.hash_secret(result.confirm_token)
            )
            assert record is not None
            record.expires_at = Clock.utcnow() - timedelta(seconds=1)
            await db.commit()

        async with DatabaseService.session() as db:
            with pytest.raises(ToolError, match="expired"):
                await ConfirmationService(db, admin.user_id).redeem(
                    result.confirm_token, "create_company"
                )

    async def test_commit_with_another_users_token_is_refused(self, admin, member):
        result = await self._issue(admin.user_id)
        async with DatabaseService.session() as db:
            with pytest.raises(ToolError):
                await ConfirmationService(db, member.user_id).redeem(
                    result.confirm_token, "create_company"
                )
