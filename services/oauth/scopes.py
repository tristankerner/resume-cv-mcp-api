"""What an OAuth grant may ever carry.

Deliberately narrower than the application's full scope set, for two reasons.

The first is blast radius. An OAuth access token authenticates on the whole
REST surface, not only on `/resume/mcp`, so a scope issued here is one a
connector can spend anywhere. The MCP tools are all read-only.

The second is what clients do with `scopes_supported`: a client builds its
authorization request from the metadata rather than from what it needs.
Advertising the whole enum meant Claude requesting `users:admin` along with
everything else.

Both documents advertise this set and `_validate_protocol_params` enforces it,
so a request for something outside it is narrowed away rather than honoured.

**Widening this is a deliberate act.** Adding write scopes here would let a
connector modify documents through the REST API using a token minted for the
MCP endpoint.
"""

from services.auth.scopes import Scopes
from services.document.document_types import DocumentTypeRegistry

OAUTH_ISSUABLE_SCOPES: frozenset[Scopes] = DocumentTypeRegistry.READ_SCOPES
