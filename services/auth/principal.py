from enum import StrEnum, auto

from pydantic import BaseModel

from services.auth.exceptions import AuthErrors
from services.auth.scopes import Scopes


class CredentialKind(StrEnum):
    """How the caller proved who they are."""

    JWT = auto()
    API_KEY = auto()
    # A JWT too, but minted by /oauth/token rather than /token — see
    # AuthService._authenticate_oauth. Kept distinct from JWT because it
    # carries a narrowed, audience-bound grant rather than the full set of
    # the user's role scopes.
    OAUTH = auto()


class Principal(BaseModel):
    """An authenticated caller, resolved once per request.

    Every authorization decision reads scopes off this object. Nothing
    downstream compares usernames or inspects roles, so adding a new
    credential type means teaching `AuthService.authenticate` to build one of
    these and changing nothing else.
    """

    user_id: int
    username: str
    scopes: frozenset[Scopes]
    credential: CredentialKind
    expires_at: int | None = (
        None  # unix seconds; None for credentials that don't expire
    )
    api_key_id: int | None = None  # which key acted, when credential is API_KEY

    def has_scope(self, scope: Scopes) -> bool:
        return scope in self.scopes

    def require_scope(self, scope: Scopes) -> None:
        if not self.has_scope(scope):
            raise AuthErrors.insufficient_scope(scope)

    def require_interactive(self) -> None:
        """Key management and account-recovery edits need a password login.

        A key that can mint keys makes revocation meaningless: whoever holds
        the leaked one simply issues a replacement before you disable it.
        Password, username and email are the same problem from the account's
        side — they are account-recovery fields, and a key that can touch
        them makes any scope narrowing on that key meaningless, since
        whoever holds it can just take the account over outright.
        """
        if self.credential is not CredentialKind.JWT:
            raise AuthErrors.requires_interactive_login()
