import base64
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select
from sqlalchemy.orm import DeclarativeBase, InstrumentedAttribute, defer


class SQAlchemyBase(DeclarativeBase):
    @classmethod
    def heavy_columns(cls) -> tuple[InstrumentedAttribute[Any], ...]:
        """Columns a summary view never reads.

        Overridden by the models whose rows carry large `Text` or `JSON` the
        list endpoints discard. Empty here, so a model that says nothing
        keeps fetching everything.
        """
        return ()

    @classmethod
    def light(cls, stmt: Select[Any]) -> Select[Any]:
        """`stmt`, with `heavy_columns` left on the server.

        `raiseload=True` is the point rather than a precaution: without it a
        deferred column silently issues its own SELECT on first access, which
        turns one fixed over-fetch into a per-row round trip - strictly
        worse than what this replaces. With it, the same mistake raises
        `InvalidRequestError` in a test.
        """
        heavy = cls.heavy_columns()
        if not heavy:
            return stmt
        return stmt.options(*(defer(column, raiseload=True) for column in heavy))


class Clock:
    @staticmethod
    def utcnow() -> datetime:
        """Naive UTC.

        SQLite has no timezone type and round-trips aware datetimes
        inconsistently, so every timestamp in this package is UTC by convention
        rather than by tzinfo. Comparing one of these against
        `datetime.now(UTC)` raises, which is the intended failure mode: it is
        louder than a silent offset.
        """
        return datetime.now(UTC).replace(tzinfo=None)


class Base64Url:
    """Base64url, padding stripped on the way out and restored on the way in.

    Every WebAuthn byte string this package stores or compares — a credential
    id, a COSE public key, a user handle — travels as one of these rather than
    raw bytes, so it fits a plain `String` column on both SQLite and Postgres.
    Kept here rather than in `services/auth/passkeys/`: persistence does not
    import services anywhere in this repo, and `User.ensure_webauthn_handle`
    needs it too.
    """

    @staticmethod
    def encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    @staticmethod
    def decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
