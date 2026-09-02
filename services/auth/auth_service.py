from datetime import UTC, datetime, timedelta
from typing import Annotated, ClassVar

import jwt
from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from pwdlib import PasswordHash
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from persistence.api_key import ApiKey
from persistence.auth_failure import AuthFailure
from persistence.base import utcnow
from persistence.oauth_client import OAuthClient
from persistence.user import User
from services.auth.api_keys import ApiKeyToken
from services.auth.exceptions import AuthErrors
from services.auth.lockout import (
    AccountLock,
    AccountPolicy,
    AddressPolicy,
    LockKind,
    LockStatus,
)
from services.auth.principal import CredentialKind, Principal
from services.auth.scopes import ScopeResolver
from services.database.database_service import DatabaseService
from services.service_interface import ServiceProviderInterface

from ..config.config_service import ConfigService


class AuthService(ServiceProviderInterface):
    # auto_error off so this class decides what an absent credential means —
    # "anonymous" rather than a 401 raised before a route ever runs.
    oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)
    _password_hash: ClassVar[PasswordHash] = PasswordHash.recommended()

    # How stale ApiKey.last_used_at is allowed to get, traded against a database
    # write on every authenticated request.
    LAST_USED_RESOLUTION: ClassVar[timedelta] = timedelta(minutes=1)

    def __init__(
        self,
        db: AsyncSession,
        token: str | None,
        config_service: ConfigService,
        request: Request | None = None,
    ):
        self.db = db
        self.token = token
        self.config_services = config_service
        # Optional because the MCP verifier and the tests construct this
        # directly, off any request. Only the password path reads it, and only
        # to attribute a failure to an address.
        self.request = request

    @staticmethod
    def verify_password(plain_password, hashed_password):
        return AuthService._password_hash.verify(plain_password, hashed_password)

    @staticmethod
    def get_password_hash(password):
        return AuthService._password_hash.hash(password)

    async def authenticate_user(self, username: str, password: str):
        """Verify a username and password, throttling repeated failures.

        The single place a password is checked — `/token` and the docs Basic
        login both arrive here — so the throttle cannot be walked around.

        Returns the user, or False for any ordinary refusal. A refusal *for
        being locked* raises instead, because it needs to carry the wait.
        """
        account_policy = AccountPolicy.from_settings(self.config_services.settings)
        address_policy = AddressPolicy.from_settings(self.config_services.settings)
        address = address_policy.client_address(self.request)
        now = utcnow()

        # The address gate comes first: one indexed read, and it fences off
        # both the user lookup and the deliberately slow hash below.
        existing = (
            await AuthFailure.get_by_address(self.db, address) if address else None
        )
        banned = address_policy.status(existing, now)
        if banned.is_locked:
            raise AuthErrors.account_locked(banned.retry_after_seconds)

        user = await User.get_user_by_username(self.db, username)
        # A user provisioned without a password cannot log in at all; pwdlib
        # raises rather than returning False if handed None.
        if not user or not user.password:
            # No account to count against, so an unknown username is charged to
            # the address alone. Without this, spraying one guess each across a
            # list of names would be free.
            await self._register_address_failure(address, address_policy, now)
            return False

        state = account_policy.status(user, now)
        if state.kind is LockKind.PERMANENT:
            raise AuthErrors.account_locked_permanently()
        if state.kind is LockKind.TEMPORARY:
            raise AuthErrors.account_locked(state.retry_after_seconds)

        if not self.verify_password(password, user.password):
            await self._register_login_failure(
                user, address, account_policy, address_policy, now
            )
            return False

        if not user.active:
            # The password was right, so the failure history is cleared as it
            # would be for any correct password; `active` is a separate switch
            # and throttling is not what enforces it.
            await self._clear_login_failures(user)
            return False

        await self._clear_login_failures(user)
        return user

    async def _register_login_failure(
        self,
        user: User,
        address: str | None,
        account_policy: AccountPolicy,
        address_policy: AddressPolicy,
        now: datetime,
    ) -> None:
        """Charge one failure to the account and the address, and raise if
        either has just closed.

        Raising on the attempt that trips the lock, rather than on the next
        one, is what lets the caller be told how long to wait.
        """
        locked = await User.lock_for_update(self.db, user.id)
        state = (
            account_policy.register_failure(locked, now)
            if locked is not None
            else LockStatus.OPEN
        )
        await self.db.commit()

        # Counted even when the account has just locked: an attacker who moves
        # on to the next username should not have this attempt forgiven.
        banned = await self._register_address_failure(address, address_policy, now)

        if state.kind is LockKind.PERMANENT:
            raise AuthErrors.account_locked_permanently()
        if state.kind is LockKind.TEMPORARY:
            raise AuthErrors.account_locked(state.retry_after_seconds)
        if banned.is_locked:
            raise AuthErrors.account_locked(banned.retry_after_seconds)

    async def _register_address_failure(
        self, address: str | None, policy: AddressPolicy, now: datetime
    ) -> LockStatus:
        """Charge one failure to the calling address, creating its row if new.

        Committed on its own rather than with the account's counter: two
        instances seeing an address for the first time at once both insert, and
        losing that race must not roll back the account tally.
        """
        if address is None or not policy.enabled:
            return LockStatus.OPEN

        failure = await AuthFailure.lock_for_update(self.db, address)
        if failure is None:
            failure = AuthFailure(address=address, last_failure_at=now)
            self.db.add(failure)
        state = policy.register_failure(failure, now)

        try:
            await self.db.commit()
        except IntegrityError:
            # Lost the insert race. Conceded rather than retried: one uncounted
            # failure out of twenty is not worth a retry loop on the
            # authentication path.
            await self.db.rollback()
            return LockStatus.OPEN
        return state

    async def _clear_login_failures(self, user: User) -> None:
        """Forget this user's failure history after a correct password.

        The address keeps its tally: clearing it would hand an attacker a free
        reset for the price of one account they legitimately hold.
        """
        # Checked before the lock is taken, so a clean login neither writes nor
        # locks a row. A concurrent failure landing in between only means it is
        # cleared by the next successful login.
        if (
            user.failed_login_count == 0
            and user.first_failed_login_at is None
            and user.locked_until is None
            and user.lock_count == 0
        ):
            return

        locked = await User.lock_for_update(self.db, user.id)
        if locked is None:
            return
        AccountLock.clear(locked)
        await self.db.commit()

    async def verify_current_password(self, user: User, password: str) -> None:
        """Check a password on an already-authenticated request, throttled the
        same way /token is.

        Left unthrottled, any route that takes a current password is a password
        oracle for whoever holds a token for the account. The counters are
        shared with /token, so a lockout tripped here blocks /token too.

        Raises rather than returning a bool, which is one `if` away from being
        forgotten.
        """
        account_policy = AccountPolicy.from_settings(self.config_services.settings)
        address_policy = AddressPolicy.from_settings(self.config_services.settings)
        address = address_policy.client_address(self.request)
        now = utcnow()

        state = account_policy.status(user, now)
        if state.kind is LockKind.PERMANENT:
            raise AuthErrors.account_locked_permanently()
        if state.kind is LockKind.TEMPORARY:
            raise AuthErrors.account_locked(state.retry_after_seconds)

        if not self.verify_password(password, user.password):
            await self._register_login_failure(
                user, address, account_policy, address_policy, now
            )
            raise AuthErrors.wrong_current_password()

        await self._clear_login_failures(user)

    async def change_password(
        self, user: User, current_password: str, new_password: str
    ) -> None:
        """Verify `current_password` and set `new_password`."""
        await self.verify_current_password(user, current_password)
        user.password = self.get_password_hash(new_password)
        await self.db.commit()

    def raise_if_locked(self, user: User) -> None:
        """Refuse a login step for an account that is locked right now.

        The second factor arrives as a *second request*, so a lock tripped
        between the two has to be honoured on the way in as well, or a
        challenge minted a moment before the lock walks straight around it.
        All three password surfaces redeem a challenge, so all three call this.
        """
        state = AccountPolicy.from_settings(self.config_services.settings).status(
            user, utcnow()
        )
        if state.kind is LockKind.PERMANENT:
            raise AuthErrors.account_locked_permanently()
        if state.kind is LockKind.TEMPORARY:
            raise AuthErrors.account_locked(state.retry_after_seconds)

    async def register_mfa_failure(self, user: User) -> None:
        """Charge a failed second factor to the account and the address.

        The same counters /token uses: a six-digit code is a million-wide
        space, which unthrottled is an afternoon's guessing.

        Raises AuthErrors.account_locked* when this attempt trips a lock,
        exactly as _register_login_failure does on the password path.
        """
        account_policy = AccountPolicy.from_settings(self.config_services.settings)
        address_policy = AddressPolicy.from_settings(self.config_services.settings)
        address = address_policy.client_address(self.request)
        await self._register_login_failure(
            user, address, account_policy, address_policy, utcnow()
        )

    async def clear_login_failures(self, user: User) -> None:
        """Public wrapper on `_clear_login_failures`, for the MFA second step
        on a correct code."""
        await self._clear_login_failures(user)

    def create_access_token(self, data: dict, expires_delta: timedelta | None = None):
        to_encode = data.copy()
        now = datetime.now(UTC)
        if expires_delta:
            expire = now + expires_delta
        else:
            expire = now + timedelta(minutes=15)
        to_encode.update({"exp": expire, "iat": now})
        encoded_jwt = jwt.encode(
            to_encode,
            self.config_services.settings.auth_secret_key.get_secret_value(),
            algorithm=self.config_services.settings.auth_algorithm,
        )
        return encoded_jwt

    async def authenticate(self) -> Principal | None:
        """Resolve the request's bearer token into a Principal.

        The single place a credential is turned into an identity: the HTTP
        dependencies and the MCP token verifier both come through here, so the
        two surfaces cannot drift apart on what a valid token means.

        Returns None for absent, malformed, expired, unknown, or deactivated —
        callers decide whether that is a 401 or simply an anonymous request.
        """
        if not self.token:
            return None
        if ApiKeyToken.looks_like(self.token):
            return await self._authenticate_api_key(self.token)
        if self._looks_like_oauth_access_token(self.token):
            return await self._authenticate_oauth(self.token)
        return await self._authenticate_jwt(self.token)

    @staticmethod
    def _looks_like_oauth_access_token(token: str) -> bool:
        """Peeks the unverified `token_use` claim to route dispatch.

        Trusted for routing only, never for authorization: whichever branch
        this sends the token to still verifies its own signature before
        granting anything, so a forged claim cannot mint a Principal.

        `token_use`'s *absence* selects the password-JWT path. Do not start
        setting it on a password JWT — they would silently take the OAuth path
        and lose their scopes.
        """
        try:
            payload = jwt.decode(token, options={"verify_signature": False})
        except InvalidTokenError:
            return False
        return payload.get("token_use") == "oauth_access"

    async def _authenticate_api_key(self, token: str) -> Principal | None:
        parsed = ApiKeyToken.parse(token)
        if parsed is None:
            return None
        prefix, secret = parsed

        key = await ApiKey.get_by_prefix(self.db, prefix)
        if key is None:
            return None
        if not ApiKeyToken.matches(secret, key.key_hash):
            return None

        now = utcnow()
        if not key.is_usable(now):
            return None

        user = await User.get_user_by_id(self.db, key.user_id)
        if user is None or not user.active:
            return None

        await self._touch(key, now)

        # See ScopeResolver.narrow.
        scopes = await ScopeResolver.narrow(
            self.db, ScopeResolver.parse(key.scopes), user.roles
        )
        return Principal(
            user_id=user.id,
            username=user.username,
            scopes=scopes,
            credential=CredentialKind.API_KEY,
            expires_at=(
                int(key.expires_at.replace(tzinfo=UTC).timestamp())
                if key.expires_at
                else None
            ),
            api_key_id=key.id,
        )

    async def _touch(self, key: ApiKey, now: datetime) -> None:
        """Record last use, at most once a minute.

        Written eagerly rather than with the request's own work: this is usage
        telemetry, and it should survive a request that later fails. The
        resolution cap keeps it from being a database write per call.
        """
        if (
            key.last_used_at is not None
            and now - key.last_used_at < self.LAST_USED_RESOLUTION
        ):
            return
        key.last_used_at = now
        await self.db.commit()

    async def _active_user_from_subject(self, payload: dict) -> User | None:
        """`sub` is the user id, not the username: a username is mutable, and
        keying on it meant renaming an account silently invalidated every
        token it held. RFC 7519 wants a string, so it is stored as one.
        """
        subject = payload.get("sub")
        if not isinstance(subject, str):
            return None
        try:
            user_id = int(subject)
        except ValueError:
            return None

        user = await User.get_user_by_id(self.db, user_id)
        if user is None or not user.active:
            return None
        return user

    async def _authenticate_jwt(self, token: str) -> Principal | None:
        try:
            payload = jwt.decode(
                token,
                self.config_services.settings.auth_secret_key.get_secret_value(),
                algorithms=[str(self.config_services.settings.auth_algorithm)],
            )
        except InvalidTokenError:
            return None

        # An MFA challenge and a docs-session cookie are signed with this same
        # key and carry `sub`; without this, either presented as a bearer token
        # would authenticate as its subject.
        if payload.get("token_use") is not None:
            return None

        user = await self._active_user_from_subject(payload)
        if user is None:
            return None

        # Scopes come from the user record, never from the token. A role
        # revoked mid-session takes effect on the next request instead of
        # lingering until the token expires.
        return Principal(
            user_id=user.id,
            username=user.username,
            scopes=await ScopeResolver.for_roles(self.db, user.roles),
            credential=CredentialKind.JWT,
            expires_at=payload.get("exp"),
        )

    async def _authenticate_oauth(self, token: str) -> Principal | None:
        """A JWT minted by `/oauth/token`, distinguished from a password JWT
        by `token_use`.

        Unlike `_authenticate_jwt`, this token carries scopes of its own: an
        OAuth grant is narrowed at authorize time, so trusting the user's full
        role scopes here would let a client escalate past what the resource
        owner approved — see `ScopeResolver.narrow`.

        The issuing client is looked up on every call, which is what makes
        deregistration immediate. Without it an access token outlives the
        client it was minted for by up to
        AUTH_ACCESS_TOKEN_EXPIRE_MINUTES — and deregistering a *compromised*
        client is exactly the case where that window matters, so the lookup
        is worth the same cost `_active_user_from_subject` already pays to
        make a revoked role take effect immediately.
        """
        try:
            payload = jwt.decode(
                token,
                self.config_services.settings.auth_secret_key.get_secret_value(),
                algorithms=[str(self.config_services.settings.auth_algorithm)],
                audience=self.config_services.settings.oauth_resource_url,
                # A token signed with this key for another deployment sharing
                # it cannot be replayed here.
                issuer=str(self.config_services.settings.public_base_url),
            )
        except InvalidTokenError:
            return None

        user = await self._active_user_from_subject(payload)
        if user is None:
            return None

        # RFC 9068 makes `client_id` required, so a token minted by this
        # service always carries one; a token without one is not one of ours.
        client_id = payload.get("client_id")
        if not isinstance(client_id, str):
            return None
        if await OAuthClient.get_by_client_id(self.db, client_id) is None:
            return None

        # See ScopeResolver.narrow.
        granted = ScopeResolver.parse(str(payload.get("scope", "")).split())
        scopes = await ScopeResolver.narrow(self.db, granted, user.roles)
        return Principal(
            user_id=user.id,
            username=user.username,
            scopes=scopes,
            credential=CredentialKind.OAUTH,
            expires_at=payload.get("exp"),
        )

    @staticmethod
    def get_with_deps(
        request: Request,
        db: Annotated[AsyncSession, Depends(DatabaseService.get_async_db_session)],
        token: Annotated[str | None, Depends(oauth2_scheme)],
        config_service: Annotated[ConfigService, Depends(ConfigService.get_with_deps)],
    ) -> AuthService:
        return AuthService(db, token, config_service, request)

    @staticmethod
    async def get_optional_principal(
        auth_service: Annotated[AuthService, Depends(AuthService.get_with_deps)],
    ) -> Principal | None:
        """For routes that serve anonymous callers as well as authenticated ones."""
        return await auth_service.authenticate()

    @staticmethod
    async def get_principal(
        principal: Annotated[
            Principal | None, Depends(AuthService.get_optional_principal)
        ],
    ) -> Principal:
        """For routes that require a credential. Raises 401 when there isn't one."""
        if principal is None:
            raise AuthErrors.credentials()
        return principal
