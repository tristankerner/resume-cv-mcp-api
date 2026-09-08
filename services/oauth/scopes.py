from typing import ClassVar

from services.auth.scopes import Scopes
from services.document.document_types import DocumentTypeRegistry
from services.tracking.scopes import TrackingScopes


class OAuthScopes:
    """What an OAuth grant may ever carry.

    Deliberately narrower than the application's full scope set, for two
    reasons.

    The first is blast radius. An OAuth access token authenticates on the whole
    REST surface, not only on `/resume/mcp`, so a scope issued here is one a
    connector can spend anywhere. The document MCP tools are all read-only,
    which is why document scopes stop at `*_READ`.

    The second is what clients do with `scopes_supported`: a client builds its
    authorization request from the metadata rather than from what it needs.
    Advertising the whole enum meant Claude requesting `users:admin` along with
    everything else.

    Both documents advertise this set and `_validate_protocol_params` enforces
    it, so a request for something outside it is narrowed away rather than
    honoured.

    **Widening this is a deliberate act.** Adding write scopes here would let a
    connector modify documents through the REST API using a token minted for
    the MCP endpoint.

    Tracking read **and** write are issuable, unlike the document scopes: the
    tracking MCP tools have to create companies and applications during a
    tailoring run, and requiring a separate API key just for that would mean
    the connector could not do the one job it exists for. Tracking `*:delete`,
    `audit:read`, every document write scope and `users:admin` stay
    non-issuable, so the worst a compromised connector token can do is add
    rows, not remove them or read someone else's audit trail.
    """

    ISSUABLE: ClassVar[frozenset[Scopes]] = (
        DocumentTypeRegistry.READ_SCOPES
        | TrackingScopes.READ_SCOPES
        | TrackingScopes.WRITE_SCOPES
    )
