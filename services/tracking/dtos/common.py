from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

HttpUrlText = Annotated[
    str | None, Field(default=None, max_length=2048, pattern=r"^https?://")
]
"""A URL a caller supplies and the browser client later renders as a link.

The scheme restriction is the point, not tidiness. `applications.url` and
`companies.website` are rendered as `<a href={...}>` in the web client, so a
stored `javascript:` URL is script execution in the origin holding the
session token — and tracking rows are now writable over MCP by an OAuth
connector, which makes "the user typed it themselves" untrue. Anchored with
`^` so a scheme cannot be smuggled in after a prefix.

Only request models use this. Responses keep a plain `str | None`, so a row
written before this existed still reads back rather than 500ing on the way
out; the client sanitizes at render time for the same reason.
"""


class ListEnvelope[T](BaseModel):
    """The shape every list endpoint returns — see API contract 14.2."""

    model_config = ConfigDict(extra="forbid")

    data: list[T]
    total: int
    limit: int
    offset: int


MatchKind = Literal["exact", "contains", "fuzzy"]


class DuplicateCandidate(BaseModel):
    """One near-match offered back when a create is blocked as a likely
    duplicate — see API contract 14.3."""

    model_config = ConfigDict(extra="forbid")

    id: int
    label: str
    match: MatchKind
    score: float
    hint: str | None = None


class DocumentRef(BaseModel):
    """A document name/revision pair, as embedded in `ApplicationDetail`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    revision_id: int
