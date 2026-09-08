from datetime import datetime

from sqlalchemy import CheckConstraint, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from .base import Clock, SQAlchemyBase


class AuditActor(SQAlchemyBase):
    """SQLite's stand-in for Postgres' transaction-local `app.actor_user_id`
    setting - see `bind` below and section 11.3 of the tracking plan. Created
    on both dialects for schema parity, but only ever read on SQLite; the
    Postgres audit trigger reads `current_setting` instead.
    """

    __tablename__ = "audit_actor"
    __table_args__ = (CheckConstraint("id = 1", name="ck_audit_actor_single_row"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_user_id: Mapped[int | None]
    actor_credential: Mapped[str | None]
    set_at: Mapped[datetime | None]

    @staticmethod
    async def bind(
        db: AsyncSession, user_id: int | None, credential: str | None
    ) -> None:
        """Record who is making the changes in this transaction.

        Postgres carries it as a transaction-local setting, which is
        transaction-scoped by definition and correct under concurrency.
        SQLite has no such thing, so it carries it as a single row that the
        triggers read: SQLite permits one write transaction at a time across
        the database, and this write happens inside the same transaction as
        the change it attributes, so no other writer can interleave between
        the two.

        **On SQLite this row outlives the transaction that set it**, which is
        the whole hazard: a later write to an audited table that does not bind
        an actor of its own is attributed to whoever bound last. Passing
        `user_id=None` is how a path with no authenticated actor says so, and
        every writer to an audited table must do one or the other -
        `ServiceProviderInterface.bind_audit_actor` is the entry point, and
        `tests/test_audit_log.py` holds the auth paths to it.

        Best-effort by design at the edges: a change made by a migration or by
        `psql` still carries whatever the application last bound, since
        nothing outside the application calls this.
        """
        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            await db.execute(
                text("SELECT set_config('app.actor_user_id', :value, true)"),
                {"value": "" if user_id is None else str(user_id)},
            )
            await db.execute(
                text("SELECT set_config('app.actor_credential', :value, true)"),
                {"value": credential or ""},
            )
        else:
            await db.execute(
                text(
                    "INSERT INTO audit_actor (id, actor_user_id, actor_credential, "
                    "set_at) VALUES (1, :user_id, :credential, :set_at) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "actor_user_id = excluded.actor_user_id, "
                    "actor_credential = excluded.actor_credential, "
                    "set_at = excluded.set_at"
                ),
                {
                    "user_id": user_id,
                    "credential": credential,
                    # Formatted by hand rather than passed as a raw datetime:
                    # a bound `text()` parameter goes through sqlite3's
                    # default adapter, deprecated since Python 3.12, whereas
                    # every other DateTime column here goes through
                    # SQLAlchemy's own SQLite type instead. This string is
                    # that same format, so a row read back through the ORM
                    # parses identically.
                    "set_at": Clock.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f"),
                },
            )
