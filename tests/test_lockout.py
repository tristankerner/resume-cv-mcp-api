"""Login throttling: the account ladder, the address tally, and the way out.

The policy functions are exercised directly against in-memory rows — they
touch no database by design — and the wiring is exercised through /token, so
both the rule and its application are covered without making every case pay
for an argon2 hash.
"""

from datetime import timedelta

import pytest

from persistence.auth_failure import AuthFailure
from persistence.base import utcnow
from persistence.user import User
from services.auth import lockout
from services.auth.lockout import AccountLock, AccountPolicy, AddressPolicy, LockKind
from services.config.config_service import ConfigService
from services.database.database_service import DatabaseService

# The address the test client presents. ASGITransport reports a peer, and no
# X-Forwarded-For is sent, so this is the direct-connection branch.
TEST_ADDRESS = "127.0.0.1"


@pytest.fixture
def reconfigure(monkeypatch):
    """Set throttle settings, and drop the memoised copy afterwards.

    conftest's `fresh_settings` clears the cache around the test, but fixtures
    that log in — `admin` and friends — build settings during their own setup
    and re-cache them. A fixture that only set environment variables would
    therefore be silently ignored whenever pytest happened to order it after
    one of those, which is a failure that looks like the feature not working.
    """

    def apply(**values):
        for key, value in values.items():
            monkeypatch.setenv(key, str(value))
        ConfigService.reset()

    return apply


@pytest.fixture
def quick_lockout(reconfigure):
    """Three strikes, a one-minute lock, permanent on the third lock.

    Small enough to reach in a test without a hundred requests, and the
    permanent threshold is deliberately low so the ladder's top rung is
    reachable too.
    """
    reconfigure(
        AUTH_LOCKOUT_MAX_ATTEMPTS=3,
        AUTH_LOCKOUT_WINDOW_MINUTES=15,
        AUTH_LOCKOUT_BASE_MINUTES=1,
        AUTH_LOCKOUT_PERMANENT_AFTER_LOCKS=3,
    )


@pytest.fixture
def no_address_throttle(reconfigure):
    """Isolate the account ladder from the address tally.

    Without this, a test that fails a login twenty times trips the address ban
    partway through and starts asserting about the wrong mechanism.
    """
    reconfigure(AUTH_IP_MAX_FAILURES=10000)


@pytest.fixture
def policy() -> AccountPolicy:
    return AccountPolicy(
        enabled=True,
        max_attempts=3,
        window=timedelta(minutes=15),
        base_lock=timedelta(minutes=1),
        permanent_after_locks=3,
    )


@pytest.fixture
def address_policy() -> AddressPolicy:
    return AddressPolicy(
        enabled=True,
        max_failures=3,
        window=timedelta(minutes=15),
        ban=timedelta(minutes=5),
        trusted_proxy_hops=1,
    )


def fresh_user() -> User:
    """An unsaved row, which is all the policy functions need."""
    return User(username="someone", password="hash", roles=[])


async def fail_login(client, username: str, times: int = 1):
    """Post a wrong password, and hand back the last response."""
    response = None
    for _ in range(times):
        response = await client.post(
            "/token", data={"username": username, "password": "not-the-password"}
        )
    return response


async def read_user(user_id: int) -> User:
    async with DatabaseService.session() as db:
        user = await User.get_user_by_id(db, user_id)
        assert user is not None
        return user


class TestAccountPolicy:
    def test_failures_below_the_limit_do_not_lock(self, policy):
        user = fresh_user()
        now = utcnow()
        for _ in range(policy.max_attempts - 1):
            assert policy.register_failure(user, now).kind is LockKind.OPEN
        assert user.failed_login_count == policy.max_attempts - 1

    def test_the_limit_trips_a_temporary_lock(self, policy):
        user = fresh_user()
        now = utcnow()
        for _ in range(policy.max_attempts - 1):
            policy.register_failure(user, now)
        status = policy.register_failure(user, now)

        assert status.kind is LockKind.TEMPORARY
        assert status.retry_after == policy.base_lock
        assert user.locked_until == now + policy.base_lock

    def test_the_tally_resets_once_the_window_lapses(self, policy):
        user = fresh_user()
        start = utcnow()
        for _ in range(policy.max_attempts - 1):
            policy.register_failure(user, start)

        later = start + policy.window + timedelta(seconds=1)
        assert policy.register_failure(user, later).kind is LockKind.OPEN
        assert user.failed_login_count == 1

    def test_each_further_lock_doubles(self, policy):
        """One minute, then two, then permanent at the third."""
        user = fresh_user()
        now = utcnow()
        durations = []

        for lock in range(policy.permanent_after_locks):
            now = now + timedelta(minutes=30)
            for _ in range(policy.max_attempts - 1):
                policy.register_failure(user, now)
            status = policy.register_failure(user, now)
            durations.append(status)
            assert user.lock_count == lock + 1

        assert durations[0].retry_after == timedelta(minutes=1)
        assert durations[1].retry_after == timedelta(minutes=2)
        assert durations[2].kind is LockKind.PERMANENT

    def test_a_permanent_lock_clears_the_temporary_one(self, policy):
        user = fresh_user()
        now = utcnow()
        # The jump has to sit between locks, not inside a run of failures:
        # moving time on every attempt lapses the window each time and the
        # tally never reaches the limit.
        for _ in range(policy.permanent_after_locks):
            now = now + timedelta(minutes=30)
            for _ in range(policy.max_attempts):
                policy.register_failure(user, now)

        assert user.locked_permanently_at is not None
        assert user.locked_until is None
        assert policy.status(user, now).kind is LockKind.PERMANENT

    def test_a_lapsed_lock_reopens_the_account(self, policy):
        user = fresh_user()
        now = utcnow()
        for _ in range(policy.max_attempts):
            policy.register_failure(user, now)

        assert policy.status(user, now).kind is LockKind.TEMPORARY
        after = now + policy.base_lock + timedelta(seconds=1)
        assert policy.status(user, after).kind is LockKind.OPEN

    def test_a_successful_login_puts_the_ladder_back_on_the_bottom_rung(self, policy):
        """The property that keeps a stranger from walking a live account up to
        a permanent lock: every real login resets the escalation."""
        user = fresh_user()
        now = utcnow()
        for _ in range(policy.max_attempts):
            policy.register_failure(user, now)
        assert user.lock_count == 1

        AccountLock.clear(user)
        assert user.lock_count == 0
        assert user.locked_until is None
        assert user.failed_login_count == 0

        now = now + timedelta(minutes=30)
        for _ in range(policy.max_attempts - 1):
            policy.register_failure(user, now)
        status = policy.register_failure(user, now)
        assert status.retry_after == policy.base_lock

    def test_a_failure_during_a_lock_does_not_extend_it(self, policy):
        """Otherwise an attacker who keeps hammering a locked account keeps
        pushing the owner's own way back in further out."""
        user = fresh_user()
        now = utcnow()
        for _ in range(policy.max_attempts):
            policy.register_failure(user, now)
        locked_until = user.locked_until
        lock_count = user.lock_count

        for _ in range(policy.max_attempts * 2):
            status = policy.register_failure(user, now)
            assert status.kind is LockKind.TEMPORARY

        assert user.locked_until == locked_until
        assert user.lock_count == lock_count
        assert user.failed_login_count == 0

    def test_a_permanent_status_carries_no_wait(self, policy):
        """There is nothing to put in Retry-After, and the header is omitted
        rather than sent as zero."""
        user = fresh_user()
        user.locked_permanently_at = utcnow()
        status = policy.status(user, utcnow())
        assert status.retry_after is None
        assert status.retry_after_seconds == 0

    def test_unlock_clears_a_permanent_lock(self, policy):
        user = fresh_user()
        user.locked_permanently_at = utcnow()
        AccountLock.unlock(user)
        assert user.locked_permanently_at is None
        assert policy.status(user, utcnow()).kind is LockKind.OPEN

    def test_disabled_policy_never_locks(self, policy):
        disabled = AccountPolicy(
            enabled=False,
            max_attempts=policy.max_attempts,
            window=policy.window,
            base_lock=policy.base_lock,
            permanent_after_locks=policy.permanent_after_locks,
        )
        user = fresh_user()
        now = utcnow()
        for _ in range(50):
            assert disabled.register_failure(user, now).kind is LockKind.OPEN
        assert user.locked_until is None

    def test_escalation_is_capped(self):
        """A permanent threshold set absurdly high must not overflow into a
        duration no arithmetic can represent."""
        policy = AccountPolicy(
            enabled=True,
            max_attempts=1,
            window=timedelta(minutes=15),
            base_lock=timedelta(minutes=1),
            permanent_after_locks=10_000,
        )
        assert policy.lock_duration(10_000) == policy.lock_duration(
            lockout.MAX_ESCALATION + 1
        )


class TestAddressPolicy:
    def test_the_limit_trips_a_ban(self, address_policy):
        failure = AuthFailure(address="203.0.113.7", last_failure_at=utcnow())
        now = utcnow()
        for _ in range(address_policy.max_failures - 1):
            assert address_policy.register_failure(failure, now).kind is LockKind.OPEN
        status = address_policy.register_failure(failure, now)

        assert status.kind is LockKind.TEMPORARY
        assert failure.banned_until == now + address_policy.ban

    def test_a_ban_is_never_permanent(self, address_policy):
        """An address is a lease, not an identity."""
        failure = AuthFailure(address="203.0.113.7", last_failure_at=utcnow())
        now = utcnow()
        for _ in range(address_policy.max_failures * 5):
            status = address_policy.register_failure(failure, now)
            assert status.kind is not LockKind.PERMANENT

    def test_the_tally_resets_once_the_window_lapses(self, address_policy):
        failure = AuthFailure(address="203.0.113.7", last_failure_at=utcnow())
        start = utcnow()
        for _ in range(address_policy.max_failures - 1):
            address_policy.register_failure(failure, start)

        later = start + address_policy.window + timedelta(seconds=1)
        address_policy.register_failure(failure, later)
        assert failure.failure_count == 1

    def test_a_failure_during_a_ban_does_not_extend_it(self, address_policy):
        failure = AuthFailure(address="203.0.113.7", last_failure_at=utcnow())
        now = utcnow()
        for _ in range(address_policy.max_failures):
            address_policy.register_failure(failure, now)
        banned_until = failure.banned_until

        for _ in range(address_policy.max_failures * 2):
            address_policy.register_failure(failure, now)
        assert failure.banned_until == banned_until

    def test_disabled_policy_never_bans(self, address_policy):
        disabled = AddressPolicy(
            enabled=False,
            max_failures=address_policy.max_failures,
            window=address_policy.window,
            ban=address_policy.ban,
            trusted_proxy_hops=1,
        )
        failure = AuthFailure(address="203.0.113.7")
        now = utcnow()
        for _ in range(50):
            assert disabled.register_failure(failure, now).kind is LockKind.OPEN
        assert failure.banned_until is None

    def test_the_counters_default_without_a_flush(self):
        """The policy functions are meant to run on a row that was never
        saved, which needs the defaults at construction rather than at insert."""
        failure = AuthFailure(address="203.0.113.7")
        assert failure.failure_count == 0
        assert failure.last_failure_at is not None
        assert fresh_user().lock_count == 0


class TestClientAddress:
    """X-Forwarded-For is attacker-influenced to the left of our own hops."""

    def test_a_spoofed_prefix_is_ignored(self, address_policy, make_request):
        request = make_request({"x-forwarded-for": "1.1.1.1, 2.2.2.2, 9.9.9.9"})
        assert address_policy.client_address(request) == "9.9.9.9"

    def test_two_trusted_hops_reach_further_left(self, address_policy, make_request):
        policy = AddressPolicy(
            enabled=True,
            max_failures=address_policy.max_failures,
            window=address_policy.window,
            ban=address_policy.ban,
            trusted_proxy_hops=2,
        )
        request = make_request({"x-forwarded-for": "1.1.1.1, 2.2.2.2, 9.9.9.9"})
        assert policy.client_address(request) == "2.2.2.2"

    def test_a_header_shorter_than_the_hop_count_yields_nothing(
        self, address_policy, make_request
    ):
        """A request that did not come through the expected proxy. Better to
        attribute it to no address than to the wrong one."""
        policy = AddressPolicy(
            enabled=True,
            max_failures=address_policy.max_failures,
            window=address_policy.window,
            ban=address_policy.ban,
            trusted_proxy_hops=3,
        )
        request = make_request({"x-forwarded-for": "1.1.1.1, 2.2.2.2"})
        assert policy.client_address(request) is None

    def test_no_header_falls_back_to_the_peer(self, address_policy, make_request):
        assert address_policy.client_address(make_request({})) == "198.51.100.4"

    def test_zero_hops_disables_the_mechanism(self, address_policy, make_request):
        policy = AddressPolicy(
            enabled=False,
            max_failures=address_policy.max_failures,
            window=address_policy.window,
            ban=address_policy.ban,
            trusted_proxy_hops=0,
        )
        request = make_request({"x-forwarded-for": "1.1.1.1"})
        assert policy.client_address(request) is None


@pytest.fixture
def make_request():
    """A Starlette Request over a hand-built scope, headers and peer included."""
    from starlette.requests import Request

    def factory(headers: dict[str, str]):
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/token",
                "headers": [
                    (key.encode(), value.encode()) for key, value in headers.items()
                ],
                "client": ("198.51.100.4", 4444),
            }
        )

    return factory


class TestTokenEndpoint:
    async def test_the_limit_returns_429_with_a_wait(
        self, client, admin, quick_lockout, no_address_throttle
    ):
        settings = ConfigService.get_without_deps().settings
        response = await fail_login(
            client, admin.username, settings.auth_lockout_max_attempts
        )

        assert response.status_code == 429
        assert int(response.headers["retry-after"]) > 0

    async def test_the_correct_password_is_refused_while_locked(
        self, client, admin, quick_lockout, no_address_throttle
    ):
        await fail_login(client, admin.username, 3)
        response = await client.post(
            "/token", data={"username": admin.username, "password": admin.password}
        )
        assert response.status_code == 429

    async def test_failures_below_the_limit_still_return_401(
        self, client, admin, quick_lockout, no_address_throttle
    ):
        response = await fail_login(client, admin.username, 2)
        assert response.status_code == 401

    async def test_a_successful_login_clears_the_tally(
        self, client, admin, quick_lockout, no_address_throttle
    ):
        await fail_login(client, admin.username, 2)
        response = await client.post(
            "/token", data={"username": admin.username, "password": admin.password}
        )
        assert response.status_code == 200

        user = await read_user(admin.user_id)
        assert user.failed_login_count == 0
        assert user.first_failed_login_at is None

    async def test_a_lapsed_lock_lets_the_owner_back_in(
        self, client, admin, quick_lockout, no_address_throttle
    ):
        await fail_login(client, admin.username, 3)

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, admin.user_id)
            assert user is not None
            user.locked_until = utcnow() - timedelta(seconds=1)
            await db.commit()

        response = await client.post(
            "/token", data={"username": admin.username, "password": admin.password}
        )
        assert response.status_code == 200

    async def test_a_permanent_lock_returns_401_not_429(
        self, client, admin, quick_lockout, no_address_throttle
    ):
        """No wait to communicate, so nothing is disclosed that a wrong
        password would not already disclose."""
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, admin.user_id)
            assert user is not None
            user.locked_permanently_at = utcnow()
            await db.commit()

        response = await client.post(
            "/token", data={"username": admin.username, "password": admin.password}
        )
        assert response.status_code == 401
        assert "retry-after" not in response.headers

    async def test_an_unknown_username_never_locks_an_account(
        self, client, quick_lockout, no_address_throttle
    ):
        """There is no row to count against; the address carries it instead."""
        response = await fail_login(client, "nobody-at-all", 5)
        assert response.status_code == 401

    async def test_the_attempt_that_makes_it_permanent_returns_401(
        self, client, admin, quick_lockout, no_address_throttle
    ):
        """The ladder's top rung, reached through the endpoint rather than set
        up by hand: the failure that trips it answers immediately."""
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, admin.user_id)
            assert user is not None
            # One lock short of permanent, with the lock itself already lapsed.
            user.lock_count = 2
            user.locked_until = utcnow() - timedelta(seconds=1)
            await db.commit()

        response = await fail_login(client, admin.username, 3)
        assert response.status_code == 401

        user = await read_user(admin.user_id)
        assert user.locked_permanently_at is not None

    async def test_the_address_ban_can_trip_on_a_wrong_password(
        self, client, admin, reconfigure
    ):
        """Both counters run on the same attempt. With the account's limit out
        of reach, the address is what closes."""
        reconfigure(AUTH_LOCKOUT_MAX_ATTEMPTS=10000, AUTH_IP_MAX_FAILURES=3)
        response = await fail_login(client, admin.username, 3)
        assert response.status_code == 429

    async def test_a_lost_insert_race_concedes_the_attempt(
        self, client, admin, monkeypatch, reconfigure
    ):
        """Two instances seeing an address for the first time at once. The
        login still refuses; only the tally gives up that one attempt."""
        from sqlalchemy.exc import IntegrityError
        from sqlalchemy.ext.asyncio import AsyncSession

        reconfigure(AUTH_IP_MAX_FAILURES=3)
        original = AsyncSession.commit
        calls = {"n": 0}

        async def flaky_commit(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError("insert", None, Exception("duplicate address"))
            return await original(self)

        monkeypatch.setattr(AsyncSession, "commit", flaky_commit)
        response = await fail_login(client, "nobody-at-all", 1)
        assert response.status_code == 401

    async def test_disabled_lockout_never_locks(self, client, admin, reconfigure):
        reconfigure(AUTH_LOCKOUT_ENABLED="false")
        response = await fail_login(client, admin.username, 10)
        assert response.status_code == 401

        user = await read_user(admin.user_id)
        assert user.locked_until is None


class TestAddressThrottleEndpoint:
    @pytest.fixture
    def quick_address_ban(self, reconfigure):
        reconfigure(
            AUTH_IP_MAX_FAILURES=3,
            AUTH_IP_BAN_MINUTES=5,
            # Kept out of the way, so what trips is unambiguously the address.
            AUTH_LOCKOUT_MAX_ATTEMPTS=10000,
        )

    async def test_spraying_unknown_usernames_bans_the_address(
        self, client, quick_address_ban
    ):
        """The case the per-account counter cannot see: one guess each across
        many names, so no single account accumulates anything."""
        for index in range(3):
            await fail_login(client, f"nobody-{index}", 1)

        response = await fail_login(client, "nobody-again", 1)
        assert response.status_code == 429

        async with DatabaseService.session() as db:
            failure = await AuthFailure.get_by_address(db, TEST_ADDRESS)
        assert failure is not None
        assert failure.banned_until is not None

    async def test_a_banned_address_cannot_use_a_correct_password(
        self, client, admin, quick_address_ban
    ):
        for index in range(4):
            await fail_login(client, f"nobody-{index}", 1)

        response = await client.post(
            "/token", data={"username": admin.username, "password": admin.password}
        )
        assert response.status_code == 429

    async def test_a_successful_login_does_not_clear_the_address(
        self, client, admin, quick_address_ban
    ):
        """One account the caller legitimately holds must not buy a free reset
        for every other name they were guessing at."""
        await fail_login(client, "nobody-at-all", 2)
        response = await client.post(
            "/token", data={"username": admin.username, "password": admin.password}
        )
        assert response.status_code == 200

        async with DatabaseService.session() as db:
            failure = await AuthFailure.get_by_address(db, TEST_ADDRESS)
        assert failure is not None
        assert failure.failure_count == 2

    async def test_prune_drops_rows_that_decide_nothing(self, client):
        async with DatabaseService.session() as db:
            db.add(
                AuthFailure(
                    address="203.0.113.9",
                    failure_count=1,
                    first_failure_at=utcnow() - timedelta(hours=2),
                    last_failure_at=utcnow() - timedelta(hours=2),
                )
            )
            db.add(
                AuthFailure(
                    address="203.0.113.10",
                    failure_count=1,
                    first_failure_at=utcnow() - timedelta(hours=2),
                    last_failure_at=utcnow() - timedelta(hours=2),
                    banned_until=utcnow() + timedelta(hours=1),
                )
            )
            await db.commit()

        async with DatabaseService.session() as db:
            removed = await AuthFailure.prune(db, timedelta(minutes=15))
        assert removed == 1

        async with DatabaseService.session() as db:
            assert await AuthFailure.get_by_address(db, "203.0.113.9") is None
            # Still banned, so still needed.
            assert await AuthFailure.get_by_address(db, "203.0.113.10") is not None


class TestDocsLoginSharesTheCounter:
    """The docs HTML login goes through the same authenticate_user, which is
    what stops it being a way around the throttle on /token."""

    @pytest.fixture
    def production(self, reconfigure):
        reconfigure(ENVIRONMENT="production")

    @staticmethod
    async def _docs_login(client, username: str, password: str):
        return await client.post(
            "/docs/login",
            data={"username": username, "password": password, "next": "/docs"},
        )

    async def test_failures_at_the_docs_count_against_the_account(
        self, client, admin, production, quick_lockout, no_address_throttle
    ):
        for _ in range(3):
            await self._docs_login(client, admin.username, "not-the-password")

        user = await read_user(admin.user_id)
        assert user.locked_until is not None

    async def test_a_locked_account_is_refused_at_the_docs(
        self, client, admin, production, quick_lockout, no_address_throttle
    ):
        await fail_login(client, admin.username, 3)
        response = await self._docs_login(client, admin.username, admin.password)
        assert response.status_code == 429


class TestBreakGlass:
    async def test_an_api_key_still_works_while_the_account_is_locked(
        self, client, admin, quick_lockout, no_address_throttle
    ):
        """The property the whole design rests on: a lock closes the password
        path only, so an owner locked out of /token can still act."""
        created = await client.post(
            "/api-keys",
            headers=admin.headers,
            json={"name": "break-glass", "scopes": ["users:admin"]},
        )
        assert created.status_code == 200, created.text
        key = created.json()["key"]

        await fail_login(client, admin.username, 3)
        assert (await fail_login(client, admin.username, 1)).status_code == 429

        response = await client.get(
            "/users/me", headers={"Authorization": f"Bearer {key}"}
        )
        assert response.status_code == 200

    async def test_the_unlock_route_reopens_the_account(
        self, client, admin, make_actor, quick_lockout, no_address_throttle
    ):
        victim = await make_actor("victim", [])
        await fail_login(client, victim.username, 3)
        assert (
            await client.post(
                "/token",
                data={"username": victim.username, "password": victim.password},
            )
        ).status_code == 429

        response = await client.delete(
            f"/users/{victim.user_id}/lock", headers=admin.headers
        )
        assert response.status_code == 204

        response = await client.post(
            "/token", data={"username": victim.username, "password": victim.password}
        )
        assert response.status_code == 200

    async def test_the_unlock_route_clears_a_permanent_lock(
        self, client, admin, make_actor
    ):
        victim = await make_actor("victim", [])
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, victim.user_id)
            assert user is not None
            user.locked_permanently_at = utcnow()
            user.lock_count = 4
            await db.commit()

        await client.delete(f"/users/{victim.user_id}/lock", headers=admin.headers)

        user = await read_user(victim.user_id)
        assert user.locked_permanently_at is None
        assert user.lock_count == 0

    async def test_unlocking_requires_users_admin(self, client, roleless, make_actor):
        victim = await make_actor("victim", [])
        response = await client.delete(
            f"/users/{victim.user_id}/lock", headers=roleless.headers
        )
        assert response.status_code == 403

    async def test_unlocking_rejects_anonymous(self, client, admin):
        response = await client.delete(f"/users/{admin.user_id}/lock")
        assert response.status_code == 401

    async def test_unlocking_an_unknown_user_is_404(self, client, admin):
        response = await client.delete(
            f"/users/{admin.user_id + 999}/lock", headers=admin.headers
        )
        assert response.status_code == 404


class TestUnlockCommand:
    async def test_it_clears_a_permanent_lock(self, admin, capsys):
        from unlock_user import UnlockCommand

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, admin.user_id)
            assert user is not None
            user.locked_permanently_at = utcnow()
            await db.commit()

        assert await UnlockCommand().run([admin.username]) == 0
        assert "Unlocked" in capsys.readouterr().out

        user = await read_user(admin.user_id)
        assert user.locked_permanently_at is None

    async def test_it_reports_an_unknown_user(self, capsys):
        from unlock_user import UnlockCommand

        assert await UnlockCommand().run(["nobody-at-all"]) == 1
        assert "No user named" in capsys.readouterr().err

    async def test_it_clears_a_banned_address(self, capsys):
        from unlock_user import UnlockCommand

        async with DatabaseService.session() as db:
            db.add(
                AuthFailure(
                    address="203.0.113.7",
                    last_failure_at=utcnow(),
                    banned_until=utcnow() + timedelta(hours=1),
                )
            )
            await db.commit()

        assert await UnlockCommand().run(["--list"]) == 0
        assert "203.0.113.7" in capsys.readouterr().out

        assert await UnlockCommand().run(["--address", "203.0.113.7"]) == 0

        async with DatabaseService.session() as db:
            assert await AuthFailure.get_by_address(db, "203.0.113.7") is None

    async def test_it_lists_what_is_locked(self, admin, capsys):
        from unlock_user import UnlockCommand

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, admin.user_id)
            assert user is not None
            user.locked_until = utcnow() + timedelta(hours=1)
            await db.commit()

        assert await UnlockCommand().run(["--list"]) == 0
        assert admin.username in capsys.readouterr().out

    async def test_it_needs_something_to_do(self, capsys):
        from unlock_user import UnlockCommand

        assert await UnlockCommand().run([]) == 1

    async def test_it_says_so_when_nothing_is_locked(self, capsys):
        from unlock_user import UnlockCommand

        assert await UnlockCommand().run(["--list"]) == 0
        assert "Nothing is locked" in capsys.readouterr().out

    async def test_it_lists_a_permanent_lock(self, admin, capsys):
        from unlock_user import UnlockCommand

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, admin.user_id)
            assert user is not None
            user.locked_permanently_at = utcnow()
            await db.commit()

        assert await UnlockCommand().run(["--list"]) == 0
        assert "locked permanently" in capsys.readouterr().out

    async def test_unlocking_an_open_account_changes_nothing(self, admin, capsys):
        from unlock_user import UnlockCommand

        assert await UnlockCommand().run([admin.username]) == 0
        assert "was not locked" in capsys.readouterr().out

    async def test_clearing_an_unknown_address_is_not_an_error(self, capsys):
        from unlock_user import UnlockCommand

        assert await UnlockCommand().run(["--address", "203.0.113.99"]) == 0
        assert "No failures recorded" in capsys.readouterr().out
