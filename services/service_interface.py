from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from persistence.audit_actor import AuditActor
from services.auth.principal import Principal


class ServiceProviderInterface(ABC):
    """A request-scoped service constructed by FastAPI dependency injection."""

    @staticmethod
    @abstractmethod
    def get_with_deps(*args: Any, **kwargs: Any) -> ServiceProviderInterface:
        """All subclasses must implement this method"""

    @staticmethod
    async def bind_audit_actor(db: AsyncSession, principal: Principal | None) -> None:
        """Record who is making this transaction's changes, for the audit
        triggers to read back - see `AuditActor.bind` and section 11.3 of the
        tracking plan.

        Shared here rather than copied into every service that touches an
        audited table (`DocumentService`, `UserService`, `ApiKeyService`, and
        every `TrackingServiceBase` subclass via `_begin_write`), so the
        dialect branch in `AuditActor.bind` has exactly one caller-facing
        entry point.

        `None` is a meaningful argument, not a guard against a missing
        credential: it records "no authenticated actor", which is the truth
        for a login-throttle counter or a bootstrap write. **Every** write to
        an audited table must call this, with `None` where there is no
        principal - on SQLite the binding is a row rather than a
        transaction-local setting, so a write that does not state its actor
        inherits whichever one was bound last. See `AuditActor.bind`.
        """
        await AuditActor.bind(
            db,
            principal.user_id if principal is not None else None,
            str(principal.credential) if principal is not None else None,
        )
