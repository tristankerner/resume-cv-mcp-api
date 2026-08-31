from __future__ import annotations

from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from persistence.mfa_credential import MfaCredential
from services.auth.mfa.kinds import MfaMethodKind
from services.auth.mfa.methods.backup_codes import BackupCodesMethod
from services.auth.mfa.methods.method import MfaMethod
from services.auth.mfa.methods.totp import TotpMethod
from services.config.config_service import ConfigServiceModel


class MfaMethodRegistry:
    """Resolves a credential kind to the object that knows how to work it.

    The repository requirement: everything outside this package names a
    `MfaMethodKind`, never a class. Adding SMS is a module in methods/ plus
    one line in `_METHODS` — no router, no service and no DTO learns a new
    name, and nothing has to be searched for a place that switches on kind,
    because this is the only one.
    """

    _METHODS: ClassVar[dict[MfaMethodKind, type[MfaMethod]]] = {
        MfaMethodKind.TOTP: TotpMethod,
        MfaMethodKind.BACKUP_CODES: BackupCodesMethod,
    }

    def __init__(self, db: AsyncSession, settings: ConfigServiceModel):
        self.db = db
        self.settings = settings

    def for_kind(self, kind: MfaMethodKind) -> MfaMethod:
        return self._METHODS[kind](self.db, self.settings)

    def for_credential(self, credential: MfaCredential) -> MfaMethod | None:
        """None for a row whose kind is no longer a method this build knows.

        Same rule as ScopeResolver.parse and for_roles: a retired method
        stops satisfying logins rather than 500ing every request from
        whoever still holds a row of that kind.
        """
        try:
            kind = MfaMethodKind(credential.kind)
        except ValueError:
            return None
        return self.for_kind(kind)

    @classmethod
    def kinds(cls) -> list[MfaMethodKind]:
        return list(cls._METHODS)

    @classmethod
    def describe(cls) -> list[dict[str, object]]:
        """What the client needs to render an "add a method" menu, without
        hardcoding the list in two languages."""
        return [
            {
                "kind": str(kind),
                "label": method.LABEL,
                "allows_multiple": method.ALLOWS_MULTIPLE,
            }
            for kind, method in cls._METHODS.items()
        ]
