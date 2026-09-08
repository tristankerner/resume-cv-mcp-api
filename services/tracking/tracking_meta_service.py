from typing import Annotated, ClassVar

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from services.auth.auth_service import AuthService
from services.auth.exceptions import AuthErrors
from services.auth.principal import Principal
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService
from services.tracking.dtos.enums import (
    ApplicationStatusValue,
    EnumsResponse,
    EnumValue,
)
from services.tracking.enums import (
    APPLICATION_STATUS_LABELS,
    APPLICATION_STATUS_TERMINAL,
    ATTACHMENT_KIND_LABELS,
    COMPANY_RELATIONSHIP_TYPE_LABELS,
    STACK_ITEM_TYPE_LABELS,
    ApplicationStatus,
    AttachmentKind,
    CompanyRelationshipType,
    StackItemType,
)
from services.tracking.tracking_service import TrackingServiceBase


class TrackingMetaService(TrackingServiceBase):
    """`GET /tracking/enums` - readable by any credential holding at least
    one of the three tracking read scopes, since the enums it serves span all
    three entities."""

    READ_SCOPES: ClassVar[frozenset[Scopes]] = frozenset(
        {Scopes.APPLICATIONS_READ, Scopes.COMPANIES_READ, Scopes.CONTACTS_READ}
    )

    def _require_any_read_scope(self) -> None:
        if self.principal is None:
            raise AuthErrors.credentials()
        if not (self.principal.scopes & self.READ_SCOPES):
            raise AuthErrors.insufficient_any_scope(
                str(scope) for scope in self.READ_SCOPES
            )

    async def get_enums(self) -> EnumsResponse:
        self._require_any_read_scope()
        return EnumsResponse(
            application_statuses=[
                ApplicationStatusValue(
                    value=status.value,
                    label=APPLICATION_STATUS_LABELS[status],
                    terminal=APPLICATION_STATUS_TERMINAL[status],
                )
                for status in ApplicationStatus
            ],
            stack_item_types=[
                EnumValue(
                    value=item_type.value, label=STACK_ITEM_TYPE_LABELS[item_type]
                )
                for item_type in StackItemType
            ],
            company_relationship_types=[
                EnumValue(
                    value=relationship_type.value,
                    label=COMPANY_RELATIONSHIP_TYPE_LABELS[relationship_type],
                )
                for relationship_type in CompanyRelationshipType
            ],
            attachment_kinds=[
                EnumValue(value=kind.value, label=ATTACHMENT_KIND_LABELS[kind])
                for kind in AttachmentKind
            ],
        )

    @staticmethod
    def get_with_deps(
        db: Annotated[AsyncSession, Depends(DatabaseService.get_async_db_session)],
        principal: Annotated[
            Principal | None, Depends(AuthService.get_optional_principal)
        ],
    ) -> TrackingMetaService:
        return TrackingMetaService(db, principal)
