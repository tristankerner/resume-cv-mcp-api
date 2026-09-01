from enum import StrEnum, auto


class Roles(StrEnum):
    """Who a user is. What that entitles them to lives in `role_scopes`.

    Two roles, differing in one scope: an admin may administer users, a member
    may not. Everything about documents is granted to both, since ownership —
    not the role — is what keeps a member out of anyone else's.

    There is no `mcp` role. Now that a document belongs to a user, an MCP
    client authenticates as that user with an API key narrowed to the scopes it
    needs, which is finer-grained than a role and revocable on its own.
    """

    ADMIN = auto()
    MEMBER = auto()
