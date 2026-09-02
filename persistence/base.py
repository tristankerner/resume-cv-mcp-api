from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


class SQAlchemyBase(DeclarativeBase):
    pass


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
