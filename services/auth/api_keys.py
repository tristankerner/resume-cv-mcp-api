"""Token format for API keys: generation, parsing, and comparison.

Deliberately hashed with SHA-256 rather than the password hasher. Verification
happens on every request, so a deliberately slow KDF would be a self-inflicted
denial of service — and it would buy nothing: these secrets are 32 bytes of
system randomness, with no dictionary to attack. Slow KDFs exist for
low-entropy human passwords.
"""

import hashlib
import hmac
import secrets
from typing import ClassVar


class ApiKeyToken:
    SCHEME: ClassVar[str] = "rsm"
    SEPARATOR: ClassVar[str] = "_"
    PREFIX_BYTES: ClassVar[int] = 4  # 8 hex characters, enough to be unique, not secret
    SECRET_BYTES: ClassVar[int] = 32

    # Hex rather than base64url: token_urlsafe emits "_", which would collide
    # with the field separator and make parsing ambiguous.
    _PARTS: ClassVar[int] = 3

    @staticmethod
    def hash_secret(secret: str) -> str:
        return hashlib.sha256(secret.encode()).hexdigest()

    @classmethod
    def generate(cls) -> tuple[str, str, str]:
        """Return (full_key, prefix, key_hash). The full key is never stored."""
        prefix = secrets.token_hex(cls.PREFIX_BYTES)
        secret = secrets.token_hex(cls.SECRET_BYTES)
        return (
            cls.SEPARATOR.join((cls.SCHEME, prefix, secret)),
            prefix,
            cls.hash_secret(secret),
        )

    @classmethod
    def looks_like(cls, token: str) -> bool:
        """Cheap discriminator so a bearer token is routed to the right verifier."""
        return token.startswith(cls.SCHEME + cls.SEPARATOR)

    @classmethod
    def parse(cls, token: str) -> tuple[str, str] | None:
        """Split a presented key into (prefix, secret), or None if malformed."""
        parts = token.split(cls.SEPARATOR)
        if len(parts) != cls._PARTS:
            return None
        scheme, prefix, secret = parts
        if scheme != cls.SCHEME or not prefix or not secret:
            return None
        return prefix, secret

    @classmethod
    def matches(cls, secret: str, key_hash: str) -> bool:
        return hmac.compare_digest(cls.hash_secret(secret), key_hash)
