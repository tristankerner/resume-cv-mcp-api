from datetime import datetime

from sqlalchemy import JSON, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column

from .base import SQAlchemyBase


class User(SQAlchemyBase):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(unique=True, nullable=False)
    email: Mapped[str | None]
    first_name: Mapped[str | None]
    last_name: Mapped[str | None]
    password: Mapped[str | None]
    roles: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON))
    active: Mapped[bool] = mapped_column(default=True, nullable=False)

    # Login throttling, distinct from `active` on purpose: `active` is checked
    # on every credential, whereas these are read only where a password is
    # presented. A locked-out owner can therefore still reach the API with a
    # key they already hold and unlock themselves, which keeps a permanent lock
    # from being a kill switch any stranger can pull. See
    # services/auth/lockout.py.
    failed_login_count: Mapped[int] = mapped_column(default=0, nullable=False)
    first_failed_login_at: Mapped[datetime | None]
    locked_until: Mapped[datetime | None]
    # Consecutive locks with no successful login between them. Drives the
    # doubling, and reset by any successful login.
    lock_count: Mapped[int] = mapped_column(default=0, nullable=False)
    locked_permanently_at: Mapped[datetime | None]

    def __init__(self, **kwargs):
        """Apply the counter defaults at construction, not only at insert.

        A `mapped_column` default is evaluated during flush, so a User that
        has never been flushed carries None where the annotation promises an
        int — and `expire_on_commit=False` means an instance built by hand
        keeps that None. The throttle arithmetic in services/auth/lockout.py
        is written against the annotation and is meant to be callable on a
        row that was never saved, so the defaults are set here as well.
        """
        kwargs.setdefault("failed_login_count", 0)
        kwargs.setdefault("lock_count", 0)
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, username={self.username!r})"

    @staticmethod
    async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
        user = (
            (await db.execute(select(User).where(User.username == username)))
            .scalars()
            .first()
        )
        return user

    @staticmethod
    async def any_with_role(db: AsyncSession, role: str) -> bool:
        """Whether any user carries `role`.

        Scans rather than filters: roles is a JSON column with no index to use,
        and this is only asked at startup on a table of a handful of rows.
        """
        users = (await db.execute(select(User))).scalars().all()
        return any(role in (user.roles or []) for user in users)

    @staticmethod
    async def get_user_by_id(db: AsyncSession, id: int) -> User | None:
        user = (await db.execute(select(User).where(User.id == id))).scalars().first()
        return user

    @staticmethod
    async def lock_for_update(db: AsyncSession, id: int) -> User | None:
        """Re-read a user with the row held, for the login counter update.

        Two failed logins racing on read-modify-write would otherwise cost an
        attempt off the tally, which is an extra guess per concurrent request.
        Taken *after* the password is verified rather than around the whole
        login: holding a row lock across an argon2 hash would let a burst of
        attempts on one account pin a connection each and exhaust the pool.

        FOR UPDATE renders on Postgres and is silently dropped on SQLite,
        which has no row locks and, for the local file this runs against, no
        second process to race with.
        """
        return (
            (await db.execute(select(User).where(User.id == id).with_for_update()))
            .scalars()
            .first()
        )
