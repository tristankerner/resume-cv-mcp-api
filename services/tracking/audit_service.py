from typing import Annotated, ClassVar

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.audit_log import AuditLogEntry
from services.auth.auth_service import AuthService
from services.auth.principal import Principal
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService
from services.tracking.audit_columns import AuditColumns
from services.tracking.dtos.audit import AuditEntry
from services.tracking.dtos.common import ListEnvelope
from services.tracking.exceptions import TrackingErrors
from services.tracking.tracking_service import TrackingServiceBase


class AuditService(TrackingServiceBase):
    """`GET /audit` - own rows only, no admin override. A row whose
    `row_user_id` is NULL (an `oauth_clients` change) is therefore invisible
    to everyone over HTTP, which is correct: it is not any one user's data.
    See section 11.4 of the tracking plan.
    """

    AUDITED_TABLES: ClassVar[frozenset[str]] = frozenset(AuditColumns.AUDITED)

    @staticmethod
    def _to_dto(entry: AuditLogEntry) -> AuditEntry:
        return AuditEntry(
            id=entry.id,
            table_name=entry.table_name,
            row_pk=entry.row_pk,
            operation=entry.operation,
            changed_at=entry.changed_at,
            row_user_id=entry.row_user_id,
            actor_user_id=entry.actor_user_id,
            actor_credential=entry.actor_credential,
            changed_columns=entry.changed_columns,
            old_data=entry.old_data,
            new_data=entry.new_data,
        )

    async def search(
        self, table: str | None, row_id: str | None, limit: int, offset: int
    ) -> ListEnvelope[AuditEntry]:
        self._require(Scopes.AUDIT_READ)
        if table is not None and table not in self.AUDITED_TABLES:
            raise TrackingErrors.invalid_audit_table(sorted(self.AUDITED_TABLES))

        rows, total = await AuditLogEntry.search(
            self.db, self._owner(), table, row_id, limit, offset
        )
        data = [self._to_dto(row) for row in rows]
        return ListEnvelope(data=data, total=total, limit=limit, offset=offset)

    @staticmethod
    def get_with_deps(
        db: Annotated[AsyncSession, Depends(DatabaseService.get_async_db_session)],
        principal: Annotated[
            Principal | None, Depends(AuthService.get_optional_principal)
        ],
    ) -> AuditService:
        return AuditService(db, principal)
