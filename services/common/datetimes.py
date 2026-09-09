from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, PlainSerializer


class Utc:
    """The single conversion point between the naive-UTC datetimes every
    column in this application stores and the offset-bearing RFC 3339 the
    wire carries."""

    @staticmethod
    def normalize_in(value: datetime) -> datetime:
        """Runs *after* pydantic has parsed the input, so it only ever sees a
        `datetime` - never the raw string, epoch number or ORM attribute the
        request carried. That ordering matters: pydantic accepts a numeric
        epoch for a `datetime` field, and a validator running before it would
        be handed an `int`, raise `TypeError` trying to read it as text, and
        turn a merely-unusual request body into a 500 instead of the 422 an
        unparseable one deserves.

        Offset-bearing input collapses to the naive UTC every column expects;
        naive input is assumed to be UTC already and passes through.
        """
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value

    @staticmethod
    def serialize_out(value: datetime) -> str:
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


UtcDatetime = Annotated[
    datetime,
    AfterValidator(Utc.normalize_in),
    PlainSerializer(Utc.serialize_out, return_type=str, when_used="json"),
]
"""A `datetime` field that stores naive UTC and speaks RFC 3339 with a
literal `Z` on the wire.

Request bodies may carry any offset or none; `Utc.normalize_in` collapses
either into the naive UTC every column expects before the value reaches a
service. Response bodies always carry `Z`; `Utc.serialize_out` only runs for
JSON, so `model_dump()` keeps returning `datetime` objects and existing
assertions built against those need no change.

Pydantic's own parsing runs first and keeps its full input grammar - ISO
strings with or without an offset, and numeric epochs - with anything it
cannot parse still reported as a 422.
"""
