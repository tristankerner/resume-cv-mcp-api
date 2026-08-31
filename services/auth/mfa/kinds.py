from enum import StrEnum


class MfaMethodKind(StrEnum):
    """Which MfaMethod owns a credential row.

    Stored as a string in `mfa_credentials.kind`. An unrecognised value is
    treated as no method at all rather than raising — same rule as
    ScopeResolver.parse: a method retired from this enum should stop
    satisfying logins, not 500 every request from whoever still has a row.
    """

    TOTP = "totp"
    BACKUP_CODES = "backup_codes"
