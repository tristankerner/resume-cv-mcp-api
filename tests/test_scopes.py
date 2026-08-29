"""Scopes, DB-backed role expansion, and the Principal that carries them."""

import pytest
from fastapi import HTTPException
from sqlalchemy import delete

from persistence.role_scope import RoleScope
from services.auth.exceptions import AuthErrors
from services.auth.principal import CredentialKind, Principal
from services.auth.roles import Roles
from services.auth.scopes import ScopeResolver, Scopes
from services.database.database_service import DatabaseService


def principal(*scopes: Scopes) -> Principal:
    return Principal(
        user_id=1,
        username="someone",
        scopes=frozenset(scopes),
        credential=CredentialKind.JWT,
    )


class TestRoleExpansion:
    async def test_admin_holds_every_scope(self):
        async with DatabaseService.session() as db:
            assert await ScopeResolver.for_roles(db, [Roles.ADMIN.value]) == frozenset(
                Scopes
            )

    async def test_member_holds_every_scope_but_users_admin(self):
        """The one difference between the two roles. A member owns their own
        documents outright — ownership, not the role, is what keeps them out
        of anyone else's — so the only thing left to withhold is user
        administration."""
        async with DatabaseService.session() as db:
            granted = await ScopeResolver.for_roles(db, [Roles.MEMBER.value])
        assert granted == frozenset(Scopes) - {Scopes.USERS_ADMIN}

    async def test_the_mcp_role_is_gone(self):
        """Replaced by an API key narrowed to the scopes a client needs, which
        is finer-grained than a role and revocable on its own."""
        assert not hasattr(Roles, "MCP")
        async with DatabaseService.session() as db:
            assert await ScopeResolver.for_roles(db, ["mcp"]) == frozenset()

    async def test_each_document_type_has_its_own_scopes(self):
        """A credential can hold the tailoring instructions without the résumé."""
        async with DatabaseService.session() as db:
            granted = await ScopeResolver.for_roles(db, [Roles.MEMBER.value])
        for scope in (
            Scopes.RESUME_READ,
            Scopes.METADATA_READ,
            Scopes.SKILL_READ,
            Scopes.RESUME_WRITE,
            Scopes.METADATA_WRITE,
            Scopes.SKILL_WRITE,
            Scopes.RESUME_DELETE,
            Scopes.METADATA_DELETE,
            Scopes.SKILL_DELETE,
        ):
            assert scope in granted

    async def test_no_roles_grants_nothing(self):
        async with DatabaseService.session() as db:
            assert await ScopeResolver.for_roles(db, []) == frozenset()

    async def test_unknown_role_is_ignored(self):
        async with DatabaseService.session() as db:
            assert await ScopeResolver.for_roles(db, ["not-a-role"]) == frozenset()

    async def test_unknown_role_does_not_discard_the_known_ones(self):
        async with DatabaseService.session() as db:
            combined = await ScopeResolver.for_roles(
                db, ["not-a-role", Roles.MEMBER.value]
            )
            member_only = await ScopeResolver.for_roles(db, [Roles.MEMBER.value])
        assert combined == member_only

    async def test_roles_combine_and_dedupe(self):
        async with DatabaseService.session() as db:
            combined = await ScopeResolver.for_roles(
                db, [Roles.MEMBER.value, Roles.ADMIN.value]
            )
        assert combined == frozenset(Scopes)

    async def test_guest_is_gone(self):
        """Anonymous callers have no Principal at all, so there is no role for it."""
        assert not hasattr(Roles, "GUEST")
        async with DatabaseService.session() as db:
            assert await ScopeResolver.for_roles(db, ["guest"]) == frozenset()

    async def test_a_role_absent_from_the_table_grants_nothing(self):
        """Present in the `Roles` enum but never inserted into role_scopes —
        the table, not the enum, decides what a role grants."""
        async with DatabaseService.session() as db:
            assert (
                await ScopeResolver.for_roles(db, ["nobody-seeded-this"]) == frozenset()
            )

    async def test_an_unrecognised_scope_string_is_dropped_not_raised(self):
        """Same reasoning as ScopeResolver.parse: a scope retired from the enum must
        stop granting access, not 500 every request."""
        async with DatabaseService.session() as db:
            db.add(RoleScope(role="temp-role", scope="resume:read:sideways"))
            db.add(RoleScope(role="temp-role", scope=Scopes.RESUME_WRITE.value))
            await db.commit()
        ScopeResolver.reset_cache()
        try:
            async with DatabaseService.session() as db:
                granted = await ScopeResolver.for_roles(db, ["temp-role"])
            assert granted == frozenset({Scopes.RESUME_WRITE})
        finally:
            async with DatabaseService.session() as db:
                await db.execute(delete(RoleScope).where(RoleScope.role == "temp-role"))
                await db.commit()
            ScopeResolver.reset_cache()

    async def test_cache_reset_makes_a_change_visible(self):
        """The cache is the point; ScopeResolver.reset_cache is the escape hatch."""
        async with DatabaseService.session() as db:
            before = await ScopeResolver.for_roles(db, ["cache-test-role"])
        assert before == frozenset()

        async with DatabaseService.session() as db:
            db.add(RoleScope(role="cache-test-role", scope=Scopes.RESUME_WRITE.value))
            await db.commit()
        try:
            async with DatabaseService.session() as db:
                # Still cached, so the freshly-inserted row is not visible yet.
                stale = await ScopeResolver.for_roles(db, ["cache-test-role"])
            assert stale == frozenset()

            ScopeResolver.reset_cache()
            async with DatabaseService.session() as db:
                fresh = await ScopeResolver.for_roles(db, ["cache-test-role"])
            assert fresh == frozenset({Scopes.RESUME_WRITE})
        finally:
            async with DatabaseService.session() as db:
                await db.execute(
                    delete(RoleScope).where(RoleScope.role == "cache-test-role")
                )
                await db.commit()
            ScopeResolver.reset_cache()


class TestParseScopes:
    def test_round_trips_known_values(self):
        assert ScopeResolver.parse([Scopes.RESUME_WRITE.value]) == frozenset(
            {Scopes.RESUME_WRITE}
        )

    def test_drops_unknown_values(self):
        assert ScopeResolver.parse(["resume:read:sideways"]) == frozenset()

    def test_keeps_the_known_ones_alongside_unknown(self):
        assert ScopeResolver.parse(["nope", Scopes.RESUME_WRITE.value]) == frozenset(
            {Scopes.RESUME_WRITE}
        )

    def test_empty(self):
        assert ScopeResolver.parse([]) == frozenset()


class TestPrincipal:
    def test_has_scope(self):
        assert principal(Scopes.RESUME_WRITE).has_scope(Scopes.RESUME_WRITE)
        assert not principal(Scopes.RESUME_WRITE).has_scope(Scopes.USERS_ADMIN)

    def test_require_scope_passes_silently(self):
        principal(Scopes.RESUME_WRITE).require_scope(Scopes.RESUME_WRITE)

    def test_require_scope_raises_403_not_401(self):
        """The caller authenticated; they just may not do this."""
        with pytest.raises(HTTPException) as caught:
            principal().require_scope(Scopes.USERS_ADMIN)
        assert caught.value.status_code == 403

    def test_refusal_names_the_missing_scope(self):
        with pytest.raises(HTTPException) as caught:
            principal().require_scope(Scopes.USERS_ADMIN)
        assert Scopes.USERS_ADMIN.value in caught.value.detail

    def test_challenge_header_follows_the_bearer_spec(self):
        error = AuthErrors.insufficient_scope(Scopes.RESUME_WRITE.value)
        assert error.headers is not None
        assert 'error="insufficient_scope"' in error.headers["WWW-Authenticate"]

    def test_api_key_id_defaults_to_none(self):
        assert principal().api_key_id is None

    def test_credential_kinds(self):
        assert CredentialKind.JWT != CredentialKind.API_KEY
