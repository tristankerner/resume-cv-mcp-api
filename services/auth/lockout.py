"""Login throttling: when a password login is refused for being too frequent.

Two counters, the same shape, different keys. One hangs off the user row and
answers "has this account been guessed at"; one hangs off an address and
answers "has this caller been guessing at accounts". Neither knows about HTTP.

Both count within a *fixed* window: the tally resets once the window lapses
rather than sliding. That concedes a sustained (max_attempts - 1) per window to
a patient attacker, and costs two columns instead of a row per attempt — the
row-per-attempt design hands an attacker unbounded write amplification on the
path they are already hammering.

Account locks escalate: the first lasts `base_lock`, each further lock without
a successful login in between doubles it, and `permanent_after_locks` makes it
permanent. A successful login clears the escalation, so an account someone
actually uses cannot be walked up to a permanent lock by an outsider — see the
README on why a permanent lock is survivable rather than a stranger's kill
switch on the owner's own API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum, auto
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from fastapi import Request

    from persistence.auth_failure import AuthFailure
    from persistence.user import User
    from services.config.config_service import ConfigServiceModel

# Guards the doubling against a nonsensically high `permanent_after_locks`. At
# sixteen the duration is already measured in years.
MAX_ESCALATION = 16


class LockKind(StrEnum):
    OPEN = auto()
    TEMPORARY = auto()
    PERMANENT = auto()


@dataclass(frozen=True)
class LockStatus:
    """The verdict on one login attempt.

    The two verdicts that carry no duration are singletons on the class rather
    than module constants. They are assigned below the class because a frozen
    dataclass cannot name instances of itself inside its own body.
    """

    # ClassVar so the dataclass machinery treats these as class attributes
    # rather than fields with no default.
    OPEN: ClassVar[LockStatus]
    PERMANENT: ClassVar[LockStatus]

    kind: LockKind
    retry_after: timedelta | None = None

    @property
    def is_locked(self) -> bool:
        return self.kind is not LockKind.OPEN

    @property
    def retry_after_seconds(self) -> int:
        """For the Retry-After header, which is whole seconds and never zero."""
        if self.retry_after is None:
            return 0
        return max(1, int(self.retry_after.total_seconds()))


LockStatus.OPEN = LockStatus(LockKind.OPEN)
LockStatus.PERMANENT = LockStatus(LockKind.PERMANENT)


@dataclass(frozen=True)
class AccountPolicy:
    enabled: bool
    max_attempts: int
    window: timedelta
    base_lock: timedelta
    permanent_after_locks: int

    @classmethod
    def from_settings(cls, settings: ConfigServiceModel) -> AccountPolicy:
        return cls(
            enabled=settings.auth_lockout_enabled,
            max_attempts=settings.auth_lockout_max_attempts,
            window=timedelta(minutes=settings.auth_lockout_window_minutes),
            base_lock=timedelta(minutes=settings.auth_lockout_base_minutes),
            permanent_after_locks=settings.auth_lockout_permanent_after_locks,
        )

    def lock_duration(self, lock_count: int) -> timedelta:
        """How long the `lock_count`-th consecutive lock lasts."""
        return self.base_lock * 2 ** min(max(lock_count - 1, 0), MAX_ESCALATION)

    def status(self, user: User, now: datetime) -> LockStatus:
        """Whether this account may attempt a password login at all.

        Read before the password is verified, so a locked account costs a row
        read rather than an argon2 hash.
        """
        if not self.enabled:
            return LockStatus.OPEN
        if user.locked_permanently_at is not None:
            return LockStatus.PERMANENT
        if user.locked_until is not None and user.locked_until > now:
            return LockStatus(LockKind.TEMPORARY, user.locked_until - now)
        return LockStatus.OPEN

    def register_failure(self, user: User, now: datetime) -> LockStatus:
        """Count one failed password attempt, and lock the account if it is due.

        Mutates `user`; the caller owns the commit.
        """
        if not self.enabled:
            return LockStatus.OPEN

        status = self.status(user, now)
        if status.is_locked:
            return status

        if (
            user.first_failed_login_at is None
            or now - user.first_failed_login_at >= self.window
        ):
            user.failed_login_count = 0
            user.first_failed_login_at = now

        user.failed_login_count += 1
        if user.failed_login_count < self.max_attempts:
            return LockStatus.OPEN

        user.lock_count += 1
        # Cleared either way: the tally that produced this lock is spent, and
        # the window after the lock lapses should start from nothing rather
        # than from a count already at the limit.
        user.failed_login_count = 0
        user.first_failed_login_at = None

        if user.lock_count >= self.permanent_after_locks:
            user.locked_permanently_at = now
            user.locked_until = None
            return LockStatus.PERMANENT

        duration = self.lock_duration(user.lock_count)
        user.locked_until = now + duration
        return LockStatus(LockKind.TEMPORARY, duration)


class AccountLock:
    """Clearing and unlocking an account. Neither takes a policy — both are
    unconditional, and `clear` is also called by the CLI break-glass path."""

    @staticmethod
    def clear(user: User) -> None:
        """Forget the failure history after a successful login.

        Resets the escalation as well as the tally, which is what keeps a
        stranger from walking a live account up to a permanent lock.
        `locked_permanently_at` is untouched because no successful login can
        happen while it is set — only `unlock` clears it.
        """
        user.failed_login_count = 0
        user.first_failed_login_at = None
        user.locked_until = None
        user.lock_count = 0

    @classmethod
    def unlock(cls, user: User) -> None:
        """Undo every lock, temporary or permanent. The admin path."""
        cls.clear(user)
        user.locked_permanently_at = None


@dataclass(frozen=True)
class AddressPolicy:
    enabled: bool
    max_failures: int
    window: timedelta
    ban: timedelta
    trusted_proxy_hops: int

    @classmethod
    def from_settings(cls, settings: ConfigServiceModel) -> AddressPolicy:
        return cls(
            # Zero hops means no proxy is known to sit in front, so
            # X-Forwarded-For is whatever the caller chose to send and keying
            # anything on it would be worse than not throttling at all.
            enabled=settings.auth_lockout_enabled
            and settings.auth_trusted_proxy_hops > 0,
            max_failures=settings.auth_ip_max_failures,
            window=timedelta(minutes=settings.auth_ip_window_minutes),
            ban=timedelta(minutes=settings.auth_ip_ban_minutes),
            trusted_proxy_hops=settings.auth_trusted_proxy_hops,
        )

    def client_address(self, request: Request | None) -> str | None:
        """The caller's address, taken a fixed number of hops from the right.

        Never the leftmost X-Forwarded-For entry: a client may send the header
        itself and the proxy in front appends rather than replaces, so
        everything left of our trusted hops is a string the attacker chose.

        Returns None when the header is too short for the configured hop count
        — a request that arrived by some route other than the expected proxy,
        whose address cannot be established.
        """
        if request is None or not self.enabled:
            return None

        forwarded = request.headers.get("x-forwarded-for")
        if not forwarded:
            # Direct connection: the peer address is the client, and there is
            # no header to distrust.
            return request.client.host if request.client else None

        hops = [entry.strip() for entry in forwarded.split(",") if entry.strip()]
        if len(hops) < self.trusted_proxy_hops:
            return None
        return hops[-self.trusted_proxy_hops]

    def status(self, failure: AuthFailure | None, now: datetime) -> LockStatus:
        """Whether this address may attempt a password login.

        Never permanent: an address is reassigned and shared behind NAT, so
        banning one forever punishes whoever holds the lease next.
        """
        if not self.enabled or failure is None:
            return LockStatus.OPEN
        if failure.banned_until is not None and failure.banned_until > now:
            return LockStatus(LockKind.TEMPORARY, failure.banned_until - now)
        return LockStatus.OPEN

    def register_failure(self, failure: AuthFailure, now: datetime) -> LockStatus:
        """Count one failed login from an address, and ban it if it is due.

        Mutates `failure`; the caller owns the commit.
        """
        if not self.enabled:
            return LockStatus.OPEN

        status = self.status(failure, now)
        if status.is_locked:
            return status

        if (
            failure.first_failure_at is None
            or now - failure.first_failure_at >= self.window
        ):
            failure.failure_count = 0
            failure.first_failure_at = now

        failure.failure_count += 1
        failure.last_failure_at = now
        if failure.failure_count < self.max_failures:
            return LockStatus.OPEN

        failure.failure_count = 0
        failure.first_failure_at = None
        failure.banned_until = now + self.ban
        return LockStatus(LockKind.TEMPORARY, self.ban)
