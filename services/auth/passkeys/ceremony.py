from __future__ import annotations

import json
from typing import ClassVar

import webauthn
from webauthn.authentication.verify_authentication_response import (
    VerifiedAuthentication,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidJSONStructure,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)
from webauthn.registration.verify_registration_response import VerifiedRegistration

from persistence.base import Base64Url
from persistence.passkey_credential import PasskeyCredential
from persistence.user import User
from services.auth.passkeys.relying_party import RelyingParty


class PasskeyCeremony:
    """Generates and verifies the two WebAuthn ceremonies.

    Holds no session and mutates nothing: options generation needs the user's
    existing credentials, which the caller passes in, and verification is a
    pure check against a challenge the caller already validated. Persisting
    the result is PasskeyService's and PasskeyLogin's job.
    """

    # ES256 first, RS256 second. Ed25519 (-8) is deliberately absent: support
    # is uneven and every authenticator this is likely to meet does ES256.
    SUPPORTED_ALGORITHMS: ClassVar[list[COSEAlgorithmIdentifier]] = [
        COSEAlgorithmIdentifier.ECDSA_SHA_256,
        COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
    ]

    # The exceptions a malformed or rejected credential dict raises from
    # py_webauthn, one tuple per ceremony, so callers catch exactly these and
    # let anything else 500 as the real bug it would be.
    REGISTRATION_FAILURE: ClassVar[tuple[type[Exception], ...]] = (
        InvalidRegistrationResponse,
        InvalidJSONStructure,
    )
    AUTHENTICATION_FAILURE: ClassVar[tuple[type[Exception], ...]] = (
        InvalidAuthenticationResponse,
        InvalidJSONStructure,
    )

    def __init__(self, relying_party: RelyingParty):
        self.relying_party = relying_party

    @staticmethod
    def known_transports(values: object) -> list[str]:
        """The subset of `values` py_webauthn's `AuthenticatorTransport` knows.

        Applied on the way in *and* on the way out. The transports array is the
        one part of a registration response nothing verifies — it rides beside
        the attestation rather than inside it, so a client may put anything
        there. Stored unfiltered, the first unknown string turns
        `authentication_options` into a `ValueError` for that account, and
        since anyone may ask for a username's options that is an
        unauthenticated 500. Filtering rather than refusing: a transport this
        build has not heard of is a hint about which prompt to show, not a
        credential, and losing it costs nothing.
        """
        if not isinstance(values, list):
            return []
        known = {transport.value for transport in AuthenticatorTransport}
        return [value for value in values if value in known]

    def registration_options(
        self, user: User, user_handle: bytes, existing: list[PasskeyCredential]
    ) -> tuple[dict, bytes]:
        """The options dict for the browser, and the raw challenge bytes.

        `existing` becomes `excludeCredentials`, which is what makes a browser
        refuse to enrol the same authenticator twice rather than silently
        creating a second credential the user cannot tell apart from the
        first.
        """
        options = webauthn.generate_registration_options(
            rp_id=self.relying_party.rp_id,
            rp_name=self.relying_party.rp_name,
            user_id=user_handle,
            user_name=user.username,
            user_display_name=user.username,
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=Base64Url.decode(c.credential_id))
                for c in existing
            ],
            supported_pub_key_algs=self.SUPPORTED_ALGORITHMS,
            timeout=self.relying_party.timeout_ms,
        )
        return json.loads(webauthn.options_to_json(options)), options.challenge

    def verify_registration(
        self, credential: dict, challenge: bytes
    ) -> VerifiedRegistration:
        """Raises InvalidRegistrationResponse; the caller turns that into a
        401."""
        return webauthn.verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_origin=self.relying_party.origins,
            expected_rp_id=self.relying_party.rp_id,
            require_user_verification=True,
        )

    def authentication_options(
        self, allow: list[PasskeyCredential]
    ) -> tuple[dict, bytes]:
        """`allow` empty means the discoverable-credential flow — the browser
        offers whatever it holds for this RP ID.

        An empty list is also what an *unknown* username gets, which is what
        keeps this route from being a username oracle: the response is the
        same shape and size either way, and the ceremony fails at the
        assertion.
        """
        options = webauthn.generate_authentication_options(
            rp_id=self.relying_party.rp_id,
            allow_credentials=[
                PublicKeyCredentialDescriptor(
                    id=Base64Url.decode(c.credential_id),
                    transports=[
                        AuthenticatorTransport(t)
                        for t in self.known_transports(c.transports)
                    ],
                )
                for c in allow
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
            timeout=self.relying_party.timeout_ms,
        )
        return json.loads(webauthn.options_to_json(options)), options.challenge

    def verify_authentication(
        self, credential: dict, challenge: bytes, stored: PasskeyCredential
    ) -> VerifiedAuthentication:
        """Raises InvalidAuthenticationResponse; the caller turns that into a
        401."""
        return webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=self.relying_party.rp_id,
            expected_origin=self.relying_party.origins,
            credential_public_key=Base64Url.decode(stored.public_key),
            credential_current_sign_count=stored.sign_count,
            require_user_verification=True,
        )
