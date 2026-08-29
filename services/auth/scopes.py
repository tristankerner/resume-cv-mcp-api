import time
from enum import StrEnum
from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from persistence.role_scope import RoleScope


class Scopes(StrEnum):
    """What a caller may do, independent of who they are.

    Roles answer "who is this"; scopes answer "what may they do". Keeping them
    apart is what lets a single owner hand out narrowed credentials — an API
    key can carry a subset of its owner's scopes without inventing a user per
    capability, which is how a machine client is given exactly the three reads
    it needs and nothing else.

    One scope per document type per verb, rather than one `resume:*` family
    covering all three types. The three documents are not equally sensitive —
    the skill document is procedure, the metadata is schema documentation, the
    résumé is contact details and per-bullet figures — and a client that only
    follows the instructions has no business holding a credential that reads
    the résumé.

    There is no `resume:read:public`. The published projection takes no
    credential at all, so the scope that used to name it was checked nowhere
    and granted nothing, while still being mintable onto a key where it read as
    though it did something.
    """

    RESUME_READ = "resume:read"
    RESUME_WRITE = "resume:write"
    RESUME_DELETE = "resume:delete"

    METADATA_READ = "metadata:read"
    METADATA_WRITE = "metadata:write"
    METADATA_DELETE = "metadata:delete"

    SKILL_READ = "skill:read"
    SKILL_WRITE = "skill:write"
    SKILL_DELETE = "skill:delete"

    USERS_ADMIN = "users:admin"


class ScopeResolver:
    """Turns stored scope strings and roles into `Scopes`.

    The role -> scopes mapping is effectively static — it changes by migration
    or by hand, never by the API — and `for_roles` is called on every
    authenticated request. A TTL cache trades that staleness bound for not
    hitting the database each time; 60 seconds is short enough that an
    operator editing the table by hand does not wonder whether it took.
    """

    CACHE_SECONDS: ClassVar[int] = 60
    _cache: ClassVar[dict[str, frozenset[Scopes]] | None] = None
    _loaded_at: ClassVar[float] = 0.0

    @classmethod
    def parse(cls, values: list[str]) -> frozenset[Scopes]:
        """Turn stored scope strings into Scopes, dropping any that no longer exist.

        Same reasoning as `for_roles`: a scope retired from the enum should
        stop granting access, not break every credential that still records it.
        """
        parsed: set[Scopes] = set()
        for value in values:
            try:
                parsed.add(Scopes(value))
            except ValueError:
                continue
        return frozenset(parsed)

    @classmethod
    def reset_cache(cls) -> None:
        cls._cache = None
        cls._loaded_at = 0.0

    @classmethod
    async def _role_scope_map(cls, db: AsyncSession) -> dict[str, frozenset[Scopes]]:
        now = time.monotonic()
        if cls._cache is None or now - cls._loaded_at > cls.CACHE_SECONDS:
            grouped: dict[str, set[Scopes]] = {}
            for role, scope in await RoleScope.all_pairs(db):
                # An unrecognised scope string in the table is dropped rather
                # than raised, for the same reason `parse` drops one from a
                # stored credential: a scope retired from the enum should stop
                # granting access, not 500 every request that touches the role
                # that held it.
                try:
                    parsed = Scopes(scope)
                except ValueError:
                    continue
                grouped.setdefault(role, set()).add(parsed)
            cls._cache = {role: frozenset(scopes) for role, scopes in grouped.items()}
            cls._loaded_at = now
        return cls._cache

    @classmethod
    async def for_roles(cls, db: AsyncSession, roles: list[str]) -> frozenset[Scopes]:
        """Expand stored role strings into the scopes they grant.

        Unknown role strings grant nothing rather than raising, so a role
        removed from the table degrades to no access instead of 500ing every
        request that touches the user who still carries it. Multiple roles
        union and dedupe.
        """
        mapping = await cls._role_scope_map(db)
        granted: set[Scopes] = set()
        for role in roles:
            granted |= mapping.get(role, frozenset())
        return frozenset(granted)

    @classmethod
    async def narrow(
        cls, db: AsyncSession, granted: frozenset[Scopes], roles: list[str]
    ) -> frozenset[Scopes]:
        """A credential may be narrowed below its owner, never widened.

        The scopes a credential carries — an API key's, an OAuth grant's — are
        intersected with the owner's *current* role scopes rather than trusted
        as stored, so revoking a role takes effect on the very next use of
        every credential the user holds instead of lingering until each one
        individually expires.

        Takes `roles` rather than a `User` so this stays the one rule about
        scopes and nothing more: the API-key path, the OAuth authentication
        path and the OAuth refresh path all apply it, and none of them should
        have to reach into another service to do so.
        """
        return granted & await cls.for_roles(db, roles)
