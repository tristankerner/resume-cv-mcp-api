from enum import StrEnum, auto


class Roles(StrEnum):
    """Who a user is. What that entitles them to lives in `role_scopes`.

    Two roles, differing in one scope: an admin may administer users, a member
    may not. Everything about documents is granted to both, because a member is
    a person with a résumé on this deployment and the whole point is that it is
    theirs — ownership, not the role, is what keeps them out of anyone else's.

    There is no longer an `mcp` role. It existed to hand a machine client a
    read-only identity of its own, back when documents were a single shared
    set; now that a document belongs to a user, an MCP client authenticates as
    that user with an API key narrowed to the scopes it needs, which is both
    finer-grained than a role and revocable on its own.
    """

    ADMIN = auto()
    MEMBER = auto()
