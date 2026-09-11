from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from persistence.base import Clock
from persistence.passkey_credential import PasskeyCredential
from persistence.user import User
from services.auth.auth_service import AuthService
from services.auth.exceptions import AuthErrors, PasskeyErrors
from services.auth.lockout import AccountPolicy, LockKind
from services.auth.passkeys.ceremony import PasskeyCeremony
from services.auth.passkeys.challenge import (
    PasskeyAuthenticationChallenge,
    PasskeyContext,
)
from services.auth.passkeys.dtos.passkey import PasskeyAuthenticationOptionsResponse
from services.auth.passkeys.relying_party import RelyingParty
from services.config.config_service import ConfigServiceModel


class PasskeyLogin:
    """Whether an assertion satisfies a login, with no Principal in hand.

    Deliberately separate from PasskeyService for the same reason MfaVerifier
    is separate from MfaService: there is no authenticated caller yet. Shared
    by all three login surfaces, which is what keeps `/token`, `/docs/login`
    and `/oauth/authorize` from drifting apart on what a valid assertion
    means.
    """

    def __init__(self, db: AsyncSession, settings: ConfigServiceModel):
        self.db = db
        self.settings = settings
        self.relying_party = RelyingParty(settings)
        self.ceremony = PasskeyCeremony(self.relying_party)

    async def options(
        self,
        username: str | None,
        context: PasskeyContext,
        binding: str | None = None,
    ) -> PasskeyAuthenticationOptionsResponse:
        """Never 404s on an unknown username, and never reveals whether one
        exists: an unknown name yields the same discoverable-credential
        options a blank one does."""
        self.relying_party.require_enabled()

        user: User | None = None
        allow: list[PasskeyCredential] = []
        if username:
            user = await User.get_user_by_username(self.db, username)
            if user is not None:
                allow = await PasskeyCredential.list_for_user(self.db, user.id)

        options, challenge = self.ceremony.authentication_options(allow)
        token, expires_in = PasskeyAuthenticationChallenge.mint(
            self.settings,
            challenge,
            user.id if user is not None else None,
            context,
            binding,
        )
        return PasskeyAuthenticationOptionsResponse(
            options=options, login_token=token, expires_in=expires_in
        )

    async def authenticate(
        self,
        auth_service: AuthService,
        login_token: str,
        credential: dict,
        context: PasskeyContext,
        binding: str | None = None,
    ) -> User:
        """The user the assertion proves, or a raised refusal.

        Every ordinary failure raises the same PasskeyErrors.rejected(): an
        unknown credential, a wrong signature, a challenge for another
        account, a replayed signature counter. Distinguishing them would tell
        an unauthenticated caller which credential ids exist.

        Throttling is charged to the calling *address* alone, never the
        account: an assertion is a signature over a server-chosen challenge,
        so a failed one buys an attacker nothing, and charging the account
        would let anyone who knows a username lock that account's *password*
        login out by posting rubbish. A **temporarily** locked account still
        lets a valid assertion through — clearing the failure history on
        success — since the lock means somebody was guessing that account's
        password, and refusing the owner's unguessable passkey would turn a
        stranger's spray into the owner's own lockout. A **permanently**
        locked account is refused regardless: that state is an
        administrative decision, not a throttle.
        """
        self.relying_party.require_enabled()

        redeemed = PasskeyAuthenticationChallenge.redeem(
            self.settings, login_token, context, binding
        )
        if redeemed is None:
            raise PasskeyErrors.challenge_expired()
        challenge, bound_user_id = redeemed

        # `credential` is a validated dict on the two JSON surfaces, but the
        # OAuth authorize form carries it as a string that `OAuthService`
        # json-decodes — and `"null"` or `"[]"` decode to something with no
        # `.get`, which is an unauthenticated 500 rather than a refusal.
        if not isinstance(credential, dict):
            raise PasskeyErrors.rejected()

        credential_id = credential.get("id")
        if not isinstance(credential_id, str):
            raise PasskeyErrors.rejected()

        stored = await PasskeyCredential.get_by_credential_id(self.db, credential_id)
        if stored is None:
            await auth_service.register_address_failure()
            raise PasskeyErrors.rejected()

        # Stops a challenge issued for account A being redeemed with account
        # B's passkey.
        if bound_user_id is not None and stored.user_id != bound_user_id:
            raise PasskeyErrors.rejected()

        user = await User.get_user_by_id(self.db, stored.user_id)
        if user is None or not user.active:
            raise PasskeyErrors.rejected()

        # WebAuthn L2 §7.2 step 6: a discoverable-credential assertion names
        # its own account via `userHandle`, and py_webauthn does not and
        # cannot check that it names *this* one — nothing else does.
        response = credential.get("response")
        user_handle = response.get("userHandle") if isinstance(response, dict) else None
        if user_handle and user_handle != user.webauthn_user_handle:
            raise PasskeyErrors.rejected()

        now = Clock.utcnow()
        state = AccountPolicy.from_settings(self.settings).status(user, now)
        if state.kind is LockKind.PERMANENT:
            raise AuthErrors.account_locked_permanently()

        try:
            verified = self.ceremony.verify_authentication(
                credential, challenge, stored
            )
        except PasskeyCeremony.AUTHENTICATION_FAILURE as error:
            await auth_service.register_address_failure()
            raise PasskeyErrors.rejected() from error

        won = await PasskeyCredential.claim_sign_count(
            stored, self.db, verified.new_sign_count, now
        )
        if not won:
            raise PasskeyErrors.rejected()

        await auth_service.clear_login_failures(user)
        await self.db.commit()
        return user
