"""What an OAuth grant may ever carry.

Deliberately narrower than the application's full scope set, for two reasons.

The first is blast radius. An OAuth access token authenticates on the whole
REST surface, not only on `/resume/mcp` — `AuthService.authenticate` resolves
it into an ordinary Principal — so a scope issued here is a scope a connector
can spend anywhere. The MCP tools are read-only (`routers/mcp.py` gates every
one on a read scope), so a grant carrying `resume:write` or `users:admin`
would give a connector reach no tool it exposes needs.

The second is what clients do with `scopes_supported`. A client builds its
authorization request from the metadata rather than from any notion of what it
needs, so whatever appears there is what gets asked for. Advertising the whole
enum meant Claude requesting `users:admin` along with everything else, and a
grant far wider than the job.

Both documents advertise this set and `_validate_protocol_params` enforces it,
so the advertisement and the behaviour cannot drift: a request for something
outside it is narrowed away rather than quietly honoured.

**Widening this is a deliberate act.** Adding write scopes here would let a
connector modify documents through the REST API using a token minted for the
MCP endpoint. If that is ever wanted, it should come with a reason recorded
next to it.
"""

from services.auth.scopes import Scopes
from services.document.document_types import DocumentTypeRegistry

OAUTH_ISSUABLE_SCOPES: frozenset[Scopes] = DocumentTypeRegistry.READ_SCOPES
