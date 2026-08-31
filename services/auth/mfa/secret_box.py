from __future__ import annotations

from typing import ClassVar

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from services.config.config_service import ConfigServiceModel


class MfaSecretBox:
    """Seals a TOTP secret for storage and opens it to check a code.

    Fernet rather than raw AES-GCM: GCM needs a nonce per encryption that
    must be stored and must never repeat, and not having to manage nonces is
    the entire reason to reach for a high-level primitive here. MultiFernet
    carries rotation — every configured key can open, the first one seals.

    Unconfigured, this is a null box: `seal` returns its input and `open`
    returns it back. That is the development default, so a local run needs no
    key material to enrol an authenticator; production requires a key —
    ConfigServiceModel's model validator refuses to start without one.
    """

    # Marks a value as sealed. Fernet tokens already begin with a version
    # byte, but reading that means knowing Fernet's wire format — an explicit
    # prefix is what lets `is_sealed` be honest without decoding anything,
    # and what lets a v2 exist later without guessing.
    PREFIX: ClassVar[str] = "v1:"

    def __init__(self, fernet: MultiFernet | None):
        self._fernet = fernet

    @classmethod
    def from_settings(cls, settings: ConfigServiceModel) -> MfaSecretBox:
        keys = settings.mfa_encryption_keys
        if not keys:
            return cls(None)
        return cls(MultiFernet([Fernet(key.get_secret_value()) for key in keys]))

    def seal(self, plaintext: str) -> str:
        if self._fernet is None:
            return plaintext
        return self.PREFIX + self._fernet.encrypt(plaintext.encode()).decode()

    def open(self, stored: str) -> str | None:
        """Never raises — returns None for a token that will not open.

        This runs on the login path, and an exception there is a 500 that
        tells an unauthenticated caller the deployment's key is wrong.
        `TotpMethod` turns None into an ordinary refusal.
        """
        if not self.is_sealed(stored):
            return stored
        if self._fernet is None:
            return None
        token = stored[len(self.PREFIX) :]
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            return None

    def reseal(self, stored: str) -> str:
        """Re-sealed under the primary key, for TotpMethod.verify's lazy
        rotation. Returns `stored` unchanged if it cannot be opened, so a
        failure here can never destroy a secret."""
        plaintext = self.open(stored)
        if plaintext is None:
            return stored
        return self.seal(plaintext)

    @classmethod
    def is_sealed(cls, stored: str) -> bool:
        return stored.startswith(cls.PREFIX)
