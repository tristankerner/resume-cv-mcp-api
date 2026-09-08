from sqlalchemy.ext.asyncio import AsyncSession

from services.auth.exceptions import AuthErrors
from services.auth.principal import Principal
from services.auth.scopes import Scopes
from services.service_interface import ServiceProviderInterface


class TrackingServiceBase(ServiceProviderInterface):
    """What every tracking service repeats: the scope check, the owner id,
    and the audit-actor binding. Modelled on `DocumentService._require` /
    `_owner`.

    Left abstract on `get_with_deps` - each concrete service provides its
    own, exactly like `DocumentService.get_with_deps`, since each is wired to
    a different set of scopes.
    """

    def __init__(self, db: AsyncSession, principal: Principal | None):
        self.db = db
        self.principal = principal

    def _require(self, scope: Scopes) -> None:
        """401 with no credential, 403 with the wrong one."""
        if self.principal is None:
            raise AuthErrors.credentials()
        self.principal.require_scope(scope)

    def _owner(self) -> int:
        """The caller's own id. Every query filters on it - there is no
        cross-user read path anywhere in tracking, and no admin escape
        hatch."""
        if self.principal is None:
            raise AuthErrors.credentials()
        return self.principal.user_id

    async def _begin_write(self) -> None:
        """Bind the audit actor for this transaction. Call at the top of
        every mutating method, after the scope check and before the first
        write - see section 11.3."""
        if self.principal is None:
            raise AuthErrors.credentials()
        await self.bind_audit_actor(self.db, self.principal)
