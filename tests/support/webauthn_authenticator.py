import hashlib
import json
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import ClassVar

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec


class SoftwareAuthenticator:
    """A minimal WebAuthn authenticator, in-process.

    Exists because py_webauthn checks real ES256 signatures over real
    authenticator data — there is no fixture that fakes its way past that, and
    a test suite that mocked the verification would test nothing.

    "none" attestation only, which is all this service asks for and all that
    is tractable to build by hand.
    """

    AAGUID: ClassVar[bytes] = b"\x00" * 16

    # authenticatorData flag bits (WebAuthn L2 §6.1).
    _FLAG_UP: ClassVar[int] = 0x01  # user present
    _FLAG_UV: ClassVar[int] = 0x04  # user verified
    _FLAG_BE: ClassVar[int] = 0x08  # backup eligible
    _FLAG_BS: ClassVar[int] = 0x10  # backed up
    _FLAG_AT: ClassVar[int] = 0x40  # attested credential data present

    def __init__(
        self,
        rp_id: str,
        origin: str,
        *,
        multi_device: bool = True,
        increment_sign_count: bool = False,
    ):
        self.rp_id = rp_id
        self.origin = origin
        # BE|BS together is what a synced (multi-device) passkey reports;
        # neither bit set is a single-device, hardware-bound one. A test that
        # wants `device_type == "single_device"` constructs with
        # multi_device=False instead of flipping bits inline.
        self.multi_device = multi_device
        # Most platform authenticators report a constant zero and never
        # advance — see PasskeyCredential.claim_sign_count. A test exercising
        # the replay-refusal branch instead needs a counter that moves.
        self.increment_sign_count = increment_sign_count
        self._sign_count = 0
        self.credential_id = secrets.token_bytes(32)
        self._private_key = ec.generate_private_key(ec.SECP256R1())

    def register(self, options: dict) -> dict:
        """A registration response for the given creation options.

        `options` is the browser-facing JSON dict — the first element of what
        `PasskeyCeremony.registration_options` returns — so
        `options["challenge"]` already arrived base64url-encoded, the same
        shape `navigator.credentials.create()` would hand a real browser.
        """
        client_data = self._client_data_json("webauthn.create", options["challenge"])
        auth_data = self._auth_data(flags=self._flags(attested=True))
        auth_data += self._cose_public_key()
        attestation_object = cbor2.dumps(
            {"fmt": "none", "attStmt": {}, "authData": auth_data}
        )
        return {
            "id": self._b64u(self.credential_id),
            "rawId": self._b64u(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": self._b64u(client_data),
                "attestationObject": self._b64u(attestation_object),
                "transports": ["internal", "hybrid"],
            },
            "clientExtensionResults": {},
        }

    def authenticate(self, options: dict, *, user_handle: bytes | None = None) -> dict:
        """An assertion response for the given request options."""
        client_data = self._client_data_json("webauthn.get", options["challenge"])
        auth_data = self._auth_data(flags=self._flags(attested=False))
        signature = self._private_key.sign(
            auth_data + hashlib.sha256(client_data).digest(),
            ec.ECDSA(hashes.SHA256()),
        )
        return {
            "id": self._b64u(self.credential_id),
            "rawId": self._b64u(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": self._b64u(client_data),
                "authenticatorData": self._b64u(auth_data),
                "signature": self._b64u(signature),
                "userHandle": self._b64u(user_handle) if user_handle else None,
            },
            "clientExtensionResults": {},
        }

    def _flags(self, *, attested: bool) -> int:
        flags = self._FLAG_UP | self._FLAG_UV
        if self.multi_device:
            flags |= self._FLAG_BE | self._FLAG_BS
        if attested:
            flags |= self._FLAG_AT
        return flags

    def _auth_data(self, *, flags: int) -> bytes:
        if self.increment_sign_count:
            self._sign_count += 1
        rp_id_hash = hashlib.sha256(self.rp_id.encode()).digest()
        auth_data = rp_id_hash + bytes([flags]) + self._sign_count.to_bytes(4, "big")
        if flags & self._FLAG_AT:
            auth_data += self.AAGUID
            auth_data += len(self.credential_id).to_bytes(2, "big")
            auth_data += self.credential_id
        return auth_data

    def _cose_public_key(self) -> bytes:
        numbers = self._private_key.public_key().public_numbers()
        # kty=EC2 (2), alg=ES256 (-7), crv=P-256 (1).
        return cbor2.dumps(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _client_data_json(self, credential_type: str, challenge_b64u: str) -> bytes:
        # Serialized once and reused as the exact bytes signed — re-serializing
        # with a different key order would break the signature.
        return json.dumps(
            {
                "type": credential_type,
                "challenge": challenge_b64u,
                "origin": self.origin,
                "crossOrigin": False,
            }
        ).encode()

    @staticmethod
    def _b64u(value: bytes) -> str:
        return urlsafe_b64encode(value).decode().rstrip("=")

    @staticmethod
    def b64u_decode(value: str) -> bytes:
        return urlsafe_b64decode(value + "=" * (-len(value) % 4))
