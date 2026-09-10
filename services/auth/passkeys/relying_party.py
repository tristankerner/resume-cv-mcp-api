from __future__ import annotations

from typing import ClassVar, cast

from services.auth.exceptions import PasskeyErrors
from services.config.config_service import ConfigServiceModel


class RelyingParty:
    """The RP ID and origin set a ceremony is checked against.

    A small object rather than four settings lookups scattered through the
    package: the "is this configured at all" question is asked on every route
    here, and having one place to ask it is what keeps a disabled deployment
    from 500ing somewhere instead of 404ing everywhere.
    """

    # Two minutes is long enough for someone to find their phone and short
    # enough that a forgotten prompt clears itself. Not configurable; there is
    # no deployment whose answer differs.
    TIMEOUT_MS: ClassVar[int] = 120_000

    def __init__(self, settings: ConfigServiceModel):
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return self._settings.passkeys_enabled

    def require_enabled(self) -> None:
        if not self.enabled:
            raise PasskeyErrors.not_configured()

    @property
    def rp_id(self) -> str:
        """Only valid after require_enabled. Raises otherwise."""
        self.require_enabled()
        return cast(str, self._settings.webauthn_rp_id)

    @property
    def rp_name(self) -> str:
        return self._settings.webauthn_rp_name

    @property
    def origins(self) -> list[str]:
        """Sorted, so the list py_webauthn is handed is stable across
        processes — an unordered frozenset would make an error message name a
        different origin on each run."""
        return sorted(self._settings.webauthn_allowed_origins)

    @property
    def timeout_ms(self) -> int:
        return self.TIMEOUT_MS

    def serves_origin(self, origin: str) -> bool:
        """Whether `origin` is one a ceremony may come from. Used by the
        server-rendered login pages, which are served from this API's own
        origin and must not offer a passkey button that cannot work."""
        return self.enabled and origin in self._settings.webauthn_allowed_origins
