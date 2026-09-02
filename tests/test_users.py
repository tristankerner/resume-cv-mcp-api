"""User registration, self-service updates, and the admin boundary."""

from datetime import timedelta

import pytest

from persistence.base import utcnow
from persistence.user import User
from services.auth.roles import Roles
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService
from tests.helpers import OAuthTokens


async def narrowed_api_key(client, actor, *scopes: Scopes) -> dict[str, str]:
    """Auth headers for an API key of `actor`'s holding exactly `scopes`."""
    created = await client.post(
        "/api-keys",
        headers=actor.headers,
        json={"name": "narrowed", "scopes": [scope.value for scope in scopes]},
    )
    assert created.status_code == 200, created.text
    return {"Authorization": f"Bearer {created.json()['key']}"}


async def oauth_headers(actor, *scopes: Scopes) -> dict[str, str]:
    """Auth headers for an OAuth-kind access token, minted directly rather
    than through the authorize flow — same shortcut test_oauth.py takes."""
    return await OAuthTokens.headers(actor, *scopes)


class TestCurrentUser:
    async def test_returns_the_authenticated_user(self, client, admin):
        response = await client.get("/users/me", headers=admin.headers)
        assert response.status_code == 200
        assert response.json()["username"] == admin.username
        assert response.json()["id"] == admin.user_id

    async def test_never_exposes_the_password(self, client, admin):
        assert (
            "password"
            not in (await client.get("/users/me", headers=admin.headers)).json()
        )

    async def test_reports_roles(self, client, member):
        response = await client.get("/users/me", headers=member.headers)
        assert response.json()["roles"] == [Roles.MEMBER.value]

    async def test_requires_a_credential(self, client):
        assert (await client.get("/users/me")).status_code == 401

    async def test_user_deleted_after_token_issue(self, client, roleless):
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, roleless.user_id)
            await db.delete(user)
            await db.commit()

        assert (
            await client.get("/users/me", headers=roleless.headers)
        ).status_code == 401


class TestMemberRole:
    """The one thing an admin may do that a member may not. Everything about
    documents is granted to both — ownership, not the role, is what keeps a
    member out of anyone else's."""

    async def test_a_member_cannot_create_users(self, client, member, password):
        response = await client.post(
            "/users",
            headers=member.headers,
            json={"username": "newcomer", "password": password},
        )
        assert response.status_code == 403

    async def test_a_member_cannot_mint_a_users_admin_key(self, client, member):
        """The key ceiling is the owner's role, so a member cannot escalate by
        minting themselves a credential their role does not reach."""
        response = await client.post(
            "/api-keys",
            headers=member.headers,
            json={"name": "escalate", "scopes": [Scopes.USERS_ADMIN.value]},
        )
        assert response.status_code == 403

    async def test_a_member_may_write_their_own_documents(
        self, client, member, resume_payload
    ):
        response = await client.post(
            "/documents/resume",
            headers=member.headers,
            json={"name": "resume.json", "revision_note": "v", "data": resume_payload},
        )
        assert response.status_code == 200, response.text


class TestRegistration:
    async def test_admin_creates_a_user(self, client, admin, password):
        response = await client.post(
            "/users",
            headers=admin.headers,
            json={"username": "newcomer", "password": password},
        )
        assert response.status_code == 200
        assert response.json()["id"] > 0

    async def test_created_user_can_log_in(self, client, admin, password):
        await client.post(
            "/users",
            headers=admin.headers,
            json={"username": "newcomer", "password": password},
        )
        response = await client.post(
            "/token", data={"username": "newcomer", "password": password}
        )
        assert response.status_code == 200

    async def test_roles_are_applied(self, client, admin, password):
        await client.post(
            "/users",
            headers=admin.headers,
            json={
                "username": "newcomer",
                "password": password,
                "roles": [Roles.MEMBER.value],
            },
        )
        token = (
            await client.post(
                "/token", data={"username": "newcomer", "password": password}
            )
        ).json()["access_token"]
        response = await client.get(
            "/users/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.json()["roles"] == [Roles.MEMBER.value]

    async def test_requires_users_admin(self, client, roleless, password):
        response = await client.post(
            "/users",
            headers=roleless.headers,
            json={"username": "newcomer", "password": password},
        )
        assert response.status_code == 403

    async def test_requires_a_credential(self, client, password):
        response = await client.post(
            "/users", json={"username": "newcomer", "password": password}
        )
        assert response.status_code == 401

    async def test_duplicate_username_is_a_conflict(self, client, admin, password):
        response = await client.post(
            "/users",
            headers=admin.headers,
            json={"username": admin.username, "password": password},
        )
        assert response.status_code == 409

    @pytest.mark.parametrize(
        "bad_password",
        ["Short1!", "alllowercase1!", "ALLUPPERCASE1!", "NoDigits!!", "NoSymbols123"],
    )
    async def test_password_complexity_is_enforced(self, client, admin, bad_password):
        response = await client.post(
            "/users",
            headers=admin.headers,
            json={"username": "newcomer", "password": bad_password},
        )
        assert response.status_code == 422

    async def test_disabled_true_creates_an_inactive_user(
        self, client, admin, password
    ):
        response = await client.post(
            "/users",
            headers=admin.headers,
            json={"username": "newcomer", "password": password, "disabled": True},
        )
        user_id = response.json()["id"]
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, user_id)
        assert user is not None
        assert user.active is False

    async def test_omitting_disabled_creates_an_active_user(
        self, client, admin, password
    ):
        response = await client.post(
            "/users",
            headers=admin.headers,
            json={"username": "newcomer", "password": password},
        )
        user_id = response.json()["id"]
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, user_id)
        assert user is not None
        assert user.active is True


class TestUpdates:
    async def test_user_updates_self(self, client, roleless):
        response = await client.patch(
            f"/users/{roleless.user_id}",
            headers=roleless.headers,
            json={"first_name": "Updated"},
        )
        assert response.status_code == 204
        assert (await client.get("/users/me", headers=roleless.headers)).json()[
            "first_name"
        ] == "Updated"

    async def test_admin_updates_anyone(self, client, admin, roleless):
        response = await client.patch(
            f"/users/{roleless.user_id}",
            headers=admin.headers,
            json={"email": "someone@example.invalid"},
        )
        assert response.status_code == 204

    async def test_cannot_update_another_user(self, client, roleless, admin):
        response = await client.patch(
            f"/users/{admin.user_id}",
            headers=roleless.headers,
            json={"first_name": "Nope"},
        )
        # 404 rather than 403, so this does not confirm the account exists.
        assert response.status_code == 404

    async def test_unknown_user(self, client, admin):
        response = await client.patch(
            f"/users/{admin.user_id + 10_000}",
            headers=admin.headers,
            json={"first_name": "Nobody"},
        )
        assert response.status_code == 404

    async def test_non_admin_cannot_grant_themselves_a_role(self, client, roleless):
        await client.patch(
            f"/users/{roleless.user_id}",
            headers=roleless.headers,
            json={"roles": [Roles.ADMIN.value]},
        )
        assert (await client.get("/users/me", headers=roleless.headers)).json()[
            "roles"
        ] == []

    async def test_admin_can_change_roles(self, client, admin, roleless):
        await client.patch(
            f"/users/{roleless.user_id}",
            headers=admin.headers,
            json={"roles": [Roles.MEMBER.value]},
        )
        assert (await client.get("/users/me", headers=roleless.headers)).json()[
            "roles"
        ] == [Roles.MEMBER.value]

    async def test_disabled_true_deactivates_the_account(self, client, admin, roleless):
        response = await client.patch(
            f"/users/{roleless.user_id}",
            headers=admin.headers,
            json={"disabled": True},
        )
        assert response.status_code == 204
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, roleless.user_id)
        assert user is not None
        assert user.active is False

    async def test_disabled_false_reactivates_the_account(
        self, client, admin, roleless
    ):
        await client.patch(
            f"/users/{roleless.user_id}", headers=admin.headers, json={"disabled": True}
        )
        response = await client.patch(
            f"/users/{roleless.user_id}",
            headers=admin.headers,
            json={"disabled": False},
        )
        assert response.status_code == 204
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, roleless.user_id)
        assert user is not None
        assert user.active is True

    async def test_omitting_disabled_leaves_active_unchanged(
        self, client, admin, roleless
    ):
        response = await client.patch(
            f"/users/{roleless.user_id}",
            headers=admin.headers,
            json={"first_name": "Updated"},
        )
        assert response.status_code == 204
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, roleless.user_id)
        assert user is not None
        assert user.active is True

    async def test_rename_collision_is_a_conflict(self, client, admin, roleless):
        response = await client.patch(
            f"/users/{roleless.user_id}",
            headers=roleless.headers,
            json={"username": admin.username},
        )
        assert response.status_code == 409

    async def test_rename_succeeds_when_free(self, client, roleless):
        response = await client.patch(
            f"/users/{roleless.user_id}",
            headers=roleless.headers,
            json={"username": "renamed"},
        )
        assert response.status_code == 204

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, roleless.user_id)
        assert user is not None
        assert user.username == "renamed"

    async def test_rename_does_not_invalidate_existing_tokens(
        self, client, roleless, password
    ):
        """A token's subject is the user id, which a rename does not change.

        Keying on the username meant renaming an account silently logged it out
        of every session it held.
        """
        await client.patch(
            f"/users/{roleless.user_id}",
            headers=roleless.headers,
            json={"username": "renamed"},
        )

        response = await client.get("/users/me", headers=roleless.headers)
        assert response.status_code == 200
        assert response.json()["username"] == "renamed"
        assert response.json()["id"] == roleless.user_id

    async def test_can_log_in_under_the_new_name(self, client, roleless, password):
        await client.patch(
            f"/users/{roleless.user_id}",
            headers=roleless.headers,
            json={"username": "renamed"},
        )
        assert (
            await client.post(
                "/token", data={"username": "renamed", "password": password}
            )
        ).status_code == 200

    async def test_cannot_log_in_under_the_old_name(self, client, roleless, password):
        old_name = roleless.username
        await client.patch(
            f"/users/{roleless.user_id}",
            headers=roleless.headers,
            json={"username": "renamed"},
        )
        assert (
            await client.post(
                "/token", data={"username": old_name, "password": password}
            )
        ).status_code == 401

    async def test_renaming_to_the_same_name_is_allowed(self, client, roleless):
        response = await client.patch(
            f"/users/{roleless.user_id}",
            headers=roleless.headers,
            json={"username": roleless.username},
        )
        assert response.status_code == 204

    async def test_password_change_takes_effect(self, client, roleless, password):
        new_password = password + "X9?"
        response = await client.patch(
            f"/users/{roleless.user_id}",
            headers=roleless.headers,
            json={"password": new_password, "password_retype": new_password},
        )
        assert response.status_code == 204

        assert (
            await client.post(
                "/token", data={"username": roleless.username, "password": new_password}
            )
        ).status_code == 200
        assert (
            await client.post(
                "/token", data={"username": roleless.username, "password": password}
            )
        ).status_code == 401

    async def test_mismatched_password_retype(self, client, roleless, password):
        response = await client.patch(
            f"/users/{roleless.user_id}",
            headers=roleless.headers,
            json={"password": password, "password_retype": password + "different"},
        )
        assert response.status_code == 422

    async def test_empty_update_is_rejected(self, client, roleless):
        response = await client.patch(
            f"/users/{roleless.user_id}", headers=roleless.headers, json={}
        )
        assert response.status_code == 422

    async def test_weak_new_password_is_rejected(self, client, roleless):
        response = await client.patch(
            f"/users/{roleless.user_id}",
            headers=roleless.headers,
            json={"password": "weak", "password_retype": "weak"},
        )
        assert response.status_code == 422

    async def test_requires_a_credential(self, client, roleless):
        response = await client.patch(
            f"/users/{roleless.user_id}", json={"first_name": "Nope"}
        )
        assert response.status_code == 401


class TestChangePassword:
    """POST /users/me/password — self-service, current-password-checked."""

    async def test_correct_current_password_changes_it(
        self, client, roleless, password
    ):
        new_password = password + "X9?"
        response = await client.post(
            "/users/me/password",
            headers=roleless.headers,
            json={
                "current_password": password,
                "new_password": new_password,
                "new_password_retype": new_password,
            },
        )
        assert response.status_code == 204

        assert (
            await client.post(
                "/token",
                data={"username": roleless.username, "password": new_password},
            )
        ).status_code == 200
        assert (
            await client.post(
                "/token", data={"username": roleless.username, "password": password}
            )
        ).status_code == 401

    async def test_wrong_current_password_is_refused_and_changes_nothing(
        self, client, roleless, password
    ):
        new_password = password + "X9?"
        response = await client.post(
            "/users/me/password",
            headers=roleless.headers,
            json={
                "current_password": "not-the-real-password",
                "new_password": new_password,
                "new_password_retype": new_password,
            },
        )
        assert response.status_code == 403

        assert (
            await client.post(
                "/token", data={"username": roleless.username, "password": password}
            )
        ).status_code == 200

    async def test_new_password_equal_to_current_is_rejected(
        self, client, roleless, password
    ):
        response = await client.post(
            "/users/me/password",
            headers=roleless.headers,
            json={
                "current_password": password,
                "new_password": password,
                "new_password_retype": password,
            },
        )
        assert response.status_code == 422

    async def test_retype_mismatch_is_rejected(self, client, roleless, password):
        response = await client.post(
            "/users/me/password",
            headers=roleless.headers,
            json={
                "current_password": password,
                "new_password": password + "X9?",
                "new_password_retype": password + "different",
            },
        )
        assert response.status_code == 422

    @pytest.mark.parametrize(
        "bad_password",
        ["Short1!", "alllowercase1!", "ALLUPPERCASE1!", "NoDigits!!", "NoSymbols123"],
    )
    async def test_complexity_rules_are_enforced(
        self, client, roleless, password, bad_password
    ):
        response = await client.post(
            "/users/me/password",
            headers=roleless.headers,
            json={
                "current_password": password,
                "new_password": bad_password,
                "new_password_retype": bad_password,
            },
        )
        assert response.status_code == 422

    async def test_requires_a_credential(self, client, password):
        response = await client.post(
            "/users/me/password",
            json={
                "current_password": password,
                "new_password": password + "X9?",
                "new_password_retype": password + "X9?",
            },
        )
        assert response.status_code == 401

    async def test_an_api_key_is_refused(self, client, member, password):
        headers = await narrowed_api_key(client, member, Scopes.RESUME_READ)
        response = await client.post(
            "/users/me/password",
            headers=headers,
            json={
                "current_password": password,
                "new_password": password + "X9?",
                "new_password_retype": password + "X9?",
            },
        )
        assert response.status_code == 403

    async def test_an_oauth_token_is_refused(self, client, roleless, password):
        headers = await oauth_headers(roleless)
        response = await client.post(
            "/users/me/password",
            headers=headers,
            json={
                "current_password": password,
                "new_password": password + "X9?",
                "new_password_retype": password + "X9?",
            },
        )
        assert response.status_code == 403

    async def test_does_not_sign_out_the_existing_token(
        self, client, roleless, password
    ):
        new_password = password + "X9?"
        await client.post(
            "/users/me/password",
            headers=roleless.headers,
            json={
                "current_password": password,
                "new_password": new_password,
                "new_password_retype": new_password,
            },
        )
        response = await client.get("/users/me", headers=roleless.headers)
        assert response.status_code == 200


class TestInteractiveLoginRequiredForRecoveryFields:
    """A PATCH touching password, username, email or roles needs a password
    login, closing the hole where any credential for the account — an API key,
    an OAuth token — could take it over.

    Holding `users:admin` is not an exemption, and `TestAdminKeysCannotBecomeLogins`
    below is why: it used to be, and that made every other interactive-login
    rule in this service reachable around."""

    async def test_api_key_self_patch_of_password_is_refused(
        self, client, member, password
    ):
        headers = await narrowed_api_key(client, member, Scopes.RESUME_READ)
        response = await client.patch(
            f"/users/{member.user_id}",
            headers=headers,
            json={"password": password + "X9?", "password_retype": password + "X9?"},
        )
        assert response.status_code == 403

    async def test_api_key_self_patch_of_username_is_refused(self, client, member):
        headers = await narrowed_api_key(client, member, Scopes.RESUME_READ)
        response = await client.patch(
            f"/users/{member.user_id}",
            headers=headers,
            json={"username": "renamed-by-key"},
        )
        assert response.status_code == 403

    async def test_api_key_self_patch_of_email_is_refused(self, client, member):
        headers = await narrowed_api_key(client, member, Scopes.RESUME_READ)
        response = await client.patch(
            f"/users/{member.user_id}",
            headers=headers,
            json={"email": "new@example.invalid"},
        )
        assert response.status_code == 403

    async def test_oauth_self_patch_of_password_is_refused(
        self, client, roleless, password
    ):
        headers = await oauth_headers(roleless)
        response = await client.patch(
            f"/users/{roleless.user_id}",
            headers=headers,
            json={"password": password + "X9?", "password_retype": password + "X9?"},
        )
        assert response.status_code == 403

    async def test_oauth_self_patch_of_username_is_refused(self, client, roleless):
        headers = await oauth_headers(roleless)
        response = await client.patch(
            f"/users/{roleless.user_id}",
            headers=headers,
            json={"username": "renamed-by-oauth"},
        )
        assert response.status_code == 403

    async def test_api_key_self_patch_of_first_name_still_succeeds(
        self, client, member
    ):
        """Account recovery is the boundary, not "any field": an API key
        editing something harmless is unaffected."""
        headers = await narrowed_api_key(client, member, Scopes.RESUME_READ)
        response = await client.patch(
            f"/users/{member.user_id}",
            headers=headers,
            json={"first_name": "Updated"},
        )
        assert response.status_code == 204

    async def test_admin_reset_via_patch_still_works_without_current_password(
        self, client, admin, roleless, password
    ):
        """Interactively, that is — the admin path still never asks for the
        current password, which is the whole point of it."""
        new_password = password + "X9?"
        response = await client.patch(
            f"/users/{roleless.user_id}",
            headers=admin.headers,
            json={"password": new_password, "password_retype": new_password},
        )
        assert response.status_code == 204

        assert (
            await client.post(
                "/token",
                data={"username": roleless.username, "password": new_password},
            )
        ).status_code == 200


class TestAdminKeysCannotBecomeLogins:
    """No credential that cannot log in interactively may produce one that can.

    Every `require_interactive` gate in this service rests on that. Two routes
    used to break it while holding users:admin — PATCH could set anyone's
    password, and POST /users could mint a fresh admin with a known one — so a
    stolen admin API key was one request away from a password login and
    everything those gates deny.
    """

    async def test_an_admin_key_cannot_reset_another_users_password(
        self, client, admin, roleless, password
    ):
        headers = await narrowed_api_key(client, admin, Scopes.USERS_ADMIN)
        new_password = password + "Z9?"
        response = await client.patch(
            f"/users/{roleless.user_id}",
            headers=headers,
            json={"password": new_password, "password_retype": new_password},
        )
        assert response.status_code == 403

        assert (
            await client.post(
                "/token",
                data={"username": roleless.username, "password": new_password},
            )
        ).status_code == 401

    async def test_an_admin_oauth_token_cannot_reset_another_users_password(
        self, client, admin, roleless, password
    ):
        headers = await oauth_headers(admin, Scopes.USERS_ADMIN)
        new_password = password + "Z9?"
        response = await client.patch(
            f"/users/{roleless.user_id}",
            headers=headers,
            json={"password": new_password, "password_retype": new_password},
        )
        assert response.status_code == 403

    async def test_an_admin_key_cannot_create_a_user(self, client, admin, password):
        headers = await narrowed_api_key(client, admin, Scopes.USERS_ADMIN)
        response = await client.post(
            "/users",
            headers=headers,
            json={
                "username": "backdoor",
                "password": password + "Q1?",
                "roles": [Roles.ADMIN.value],
            },
        )
        assert response.status_code == 403

        assert (
            await client.post(
                "/token", data={"username": "backdoor", "password": password + "Q1?"}
            )
        ).status_code == 401

    async def test_an_admin_key_cannot_promote_an_account(self, client, admin, member):
        """The same escalation by a different door: grant admin to an account
        whose password you already know, then log in as it."""
        headers = await narrowed_api_key(client, admin, Scopes.USERS_ADMIN)
        response = await client.patch(
            f"/users/{member.user_id}",
            headers=headers,
            json={"roles": [Roles.ADMIN.value]},
        )
        assert response.status_code == 403

    async def test_an_admin_key_may_still_clear_a_lock(self, client, admin, make_actor):
        """The one route that must stay open to a key — a locked-out admin
        holding nothing else has to be able to unlock themselves."""
        locked = await make_actor("locked-user", [])
        headers = await narrowed_api_key(client, admin, Scopes.USERS_ADMIN)
        response = await client.delete(f"/users/{locked.user_id}/lock", headers=headers)
        assert response.status_code == 204


class TestDeactivationRequiresAdmin:
    """`disabled` needs users:admin, on anyone's account including your own.

    `active` is checked on every credential, so clearing it kills the password
    login, every live token and every API key at once — and the account cannot
    then authenticate to re-enable itself. Left ungated it was a way for a
    credential narrowed to a single read scope to brick its owner, which is
    the one thing narrowing a credential is supposed to rule out.
    """

    async def test_a_narrowed_api_key_cannot_deactivate_its_owner(self, client, member):
        headers = await narrowed_api_key(client, member, Scopes.RESUME_READ)
        response = await client.patch(
            f"/users/{member.user_id}", headers=headers, json={"disabled": True}
        )
        assert response.status_code == 403

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, member.user_id)
        assert user is not None
        assert user.active is True

    async def test_an_oauth_token_cannot_deactivate_its_owner(self, client, member):
        headers = await oauth_headers(member, Scopes.RESUME_READ)
        response = await client.patch(
            f"/users/{member.user_id}", headers=headers, json={"disabled": True}
        )
        assert response.status_code == 403

    async def test_an_interactive_login_cannot_deactivate_its_own_account(
        self, client, member
    ):
        """Not even a password JWT: an admin is what is required, because
        nothing short of one can undo it."""
        response = await client.patch(
            f"/users/{member.user_id}",
            headers=member.headers,
            json={"disabled": True},
        )
        assert response.status_code == 403

    async def test_a_member_cannot_reactivate_themselves(self, client, admin, member):
        await client.patch(
            f"/users/{member.user_id}", headers=admin.headers, json={"disabled": True}
        )
        # Deactivated, so the credential is dead — 401 before authorization is
        # even reached. The point is that there is no self-service way back.
        response = await client.patch(
            f"/users/{member.user_id}",
            headers=member.headers,
            json={"disabled": False},
        )
        assert response.status_code == 401

    async def test_an_admin_api_key_may_still_deactivate(self, client, admin, roleless):
        """users:admin is the gate, not the credential kind — an admin's
        automation holding a key keeps working."""
        headers = await narrowed_api_key(client, admin, Scopes.USERS_ADMIN)
        response = await client.patch(
            f"/users/{roleless.user_id}", headers=headers, json={"disabled": True}
        )
        assert response.status_code == 204

        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, roleless.user_id)
        assert user is not None
        assert user.active is False

    async def test_other_self_edits_are_unaffected(self, client, member):
        response = await client.patch(
            f"/users/{member.user_id}",
            headers=member.headers,
            json={"first_name": "Updated"},
        )
        assert response.status_code == 204


class TestEffectiveScopes:
    """UserDto.scopes — what the client uses to gate options on permissions
    instead of discovering them by failing."""

    async def test_admin_gets_every_scope_including_users_admin(self, client, admin):
        response = await client.get("/users/me", headers=admin.headers)
        scopes = set(response.json()["scopes"])
        assert Scopes.USERS_ADMIN.value in scopes
        assert Scopes.RESUME_WRITE.value in scopes
        assert Scopes.SKILL_DELETE.value in scopes

    async def test_member_gets_document_scopes_but_not_users_admin(
        self, client, member
    ):
        response = await client.get("/users/me", headers=member.headers)
        scopes = set(response.json()["scopes"])
        assert Scopes.USERS_ADMIN.value not in scopes
        assert Scopes.RESUME_READ.value in scopes
        assert Scopes.METADATA_WRITE.value in scopes

    async def test_roleless_user_gets_no_scopes(self, client, roleless):
        response = await client.get("/users/me", headers=roleless.headers)
        assert response.json()["scopes"] == []

    async def test_a_new_role_changes_the_response(self, client, admin, roleless):
        """Mind the 60-second role-scope cache; ScopeResolver.reset_cache (via
        the fresh_role_scope_cache fixture) is what makes this visible
        immediately rather than after the TTL."""
        before = await client.get("/users/me", headers=roleless.headers)
        assert before.json()["scopes"] == []

        await client.patch(
            f"/users/{roleless.user_id}",
            headers=admin.headers,
            json={"roles": [Roles.MEMBER.value]},
        )

        after = await client.get("/users/me", headers=roleless.headers)
        assert Scopes.RESUME_READ.value in after.json()["scopes"]


class TestListUsers:
    """`GET /users` — the row an admin UI needs to render its actions,
    without a call per user."""

    async def test_admin_sees_every_user(self, client, admin, roleless):
        response = await client.get("/users", headers=admin.headers)
        assert response.status_code == 200
        usernames = {u["username"] for u in response.json()["data"]}
        assert {admin.username, roleless.username} <= usernames

    async def test_locked_is_true_for_a_live_temporary_lock(
        self, client, admin, make_actor
    ):
        victim = await make_actor("locked-victim", [])
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, victim.user_id)
            assert user is not None
            user.locked_until = utcnow() + timedelta(hours=1)
            await db.commit()

        response = await client.get("/users", headers=admin.headers)
        row = next(
            u for u in response.json()["data"] if u["username"] == victim.username
        )
        assert row["locked"] is True

    async def test_locked_is_false_once_a_lock_has_lapsed(
        self, client, admin, make_actor
    ):
        victim = await make_actor("lapsed-victim", [])
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, victim.user_id)
            assert user is not None
            user.locked_until = utcnow() - timedelta(hours=1)
            await db.commit()

        response = await client.get("/users", headers=admin.headers)
        row = next(
            u for u in response.json()["data"] if u["username"] == victim.username
        )
        assert row["locked"] is False

    async def test_locked_is_true_for_a_permanent_lock(self, client, admin, make_actor):
        victim = await make_actor("permanent-victim", [])
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, victim.user_id)
            assert user is not None
            user.locked_permanently_at = utcnow()
            await db.commit()

        response = await client.get("/users", headers=admin.headers)
        row = next(
            u for u in response.json()["data"] if u["username"] == victim.username
        )
        assert row["locked"] is True

    async def test_mfa_enrolled_is_reported(self, client, admin, enrolled):
        response = await client.get("/users", headers=admin.headers)
        row = next(
            u
            for u in response.json()["data"]
            if u["username"] == enrolled.actor.username
        )
        assert row["mfa_enrolled"] is True

    async def test_unenrolled_is_reported_false(self, client, admin, roleless):
        response = await client.get("/users", headers=admin.headers)
        row = next(
            u for u in response.json()["data"] if u["username"] == roleless.username
        )
        assert row["mfa_enrolled"] is False

    async def test_a_member_is_refused(self, client, member):
        response = await client.get("/users", headers=member.headers)
        assert response.status_code == 403

    async def test_an_admin_api_key_may_list(self, client, admin):
        """Read-only, so — unlike reset_password and reset_mfa — a key is
        enough, consistent with unlock_user."""
        headers = await narrowed_api_key(client, admin, Scopes.USERS_ADMIN)
        response = await client.get("/users", headers=headers)
        assert response.status_code == 200

    async def test_requires_a_credential(self, client):
        assert (await client.get("/users")).status_code == 401


class TestAdminResetPassword:
    """`POST /users/{id}/password` — requires users:admin and an interactive
    login, the same reasoning as reset_mfa: an admin key that can set any
    password owns every account outright."""

    async def test_admin_resets_a_members_password(
        self, client, admin, roleless, password
    ):
        new_password = password + "X9?"
        response = await client.post(
            f"/users/{roleless.user_id}/password",
            headers=admin.headers,
            json={"new_password": new_password, "new_password_retype": new_password},
        )
        assert response.status_code == 204

        assert (
            await client.post(
                "/token",
                data={"username": roleless.username, "password": new_password},
            )
        ).status_code == 200

    async def test_the_old_password_stops_working(
        self, client, admin, roleless, password
    ):
        new_password = password + "X9?"
        await client.post(
            f"/users/{roleless.user_id}/password",
            headers=admin.headers,
            json={"new_password": new_password, "new_password_retype": new_password},
        )
        assert (
            await client.post(
                "/token", data={"username": roleless.username, "password": password}
            )
        ).status_code == 401

    async def test_a_non_admin_is_refused(self, client, member, roleless, password):
        new_password = password + "X9?"
        response = await client.post(
            f"/users/{roleless.user_id}/password",
            headers=member.headers,
            json={"new_password": new_password, "new_password_retype": new_password},
        )
        assert response.status_code == 403

    async def test_an_admin_api_key_is_refused_not_interactive(
        self, client, admin, roleless, password
    ):
        headers = await narrowed_api_key(client, admin, Scopes.USERS_ADMIN)
        new_password = password + "X9?"
        response = await client.post(
            f"/users/{roleless.user_id}/password",
            headers=headers,
            json={"new_password": new_password, "new_password_retype": new_password},
        )
        assert response.status_code == 403

    async def test_an_unknown_user_is_404(self, client, admin, password):
        response = await client.post(
            f"/users/{admin.user_id + 999}/password",
            headers=admin.headers,
            json={"new_password": password, "new_password_retype": password},
        )
        assert response.status_code == 404

    async def test_policy_violation_is_422(self, client, admin, roleless):
        response = await client.post(
            f"/users/{roleless.user_id}/password",
            headers=admin.headers,
            json={"new_password": "weak", "new_password_retype": "weak"},
        )
        assert response.status_code == 422

    async def test_mismatched_retype_is_422(self, client, admin, roleless, password):
        response = await client.post(
            f"/users/{roleless.user_id}/password",
            headers=admin.headers,
            json={
                "new_password": password + "X9?",
                "new_password_retype": password + "different",
            },
        )
        assert response.status_code == 422

    async def test_requires_a_credential(self, client, roleless, password):
        response = await client.post(
            f"/users/{roleless.user_id}/password",
            json={"new_password": password, "new_password_retype": password},
        )
        assert response.status_code == 401

    async def test_does_not_require_the_current_password(
        self, client, admin, roleless, password
    ):
        """The point of this route: recovering an account whose current
        password is exactly what is unknown or unusable."""
        new_password = password + "X9?"
        response = await client.post(
            f"/users/{roleless.user_id}/password",
            headers=admin.headers,
            json={"new_password": new_password, "new_password_retype": new_password},
        )
        assert response.status_code == 204


class TestResetMfa:
    """`DELETE /users/{id}/mfa` — requires users:admin and an interactive
    login, unlike `unlock_user`, which a locked-out admin's own API key must
    still be able to reach."""

    async def test_an_admin_can_strip_another_accounts_mfa(
        self, client, admin, enrolled
    ):
        response = await client.delete(
            f"/users/{enrolled.actor.user_id}/mfa", headers=admin.headers
        )
        assert response.status_code == 204

        status = await client.get("/users/me/mfa", headers=enrolled.actor.headers)
        assert status.json()["credentials"] == []

    async def test_a_non_admin_is_refused(self, client, member, enrolled):
        response = await client.delete(
            f"/users/{enrolled.actor.user_id}/mfa", headers=member.headers
        )
        assert response.status_code == 403

    async def test_an_admin_api_key_is_refused_not_interactive(
        self, client, admin, enrolled
    ):
        """The difference from unlock_user: MFA management refuses any API
        key, admin-scoped or not, because a key that can strip second
        factors makes MFA optional service-wide for whoever steals it."""
        headers = await narrowed_api_key(client, admin, Scopes.USERS_ADMIN)
        response = await client.delete(
            f"/users/{enrolled.actor.user_id}/mfa", headers=headers
        )
        assert response.status_code == 403

    async def test_an_unknown_user_is_404(self, client, admin):
        response = await client.delete("/users/999999/mfa", headers=admin.headers)
        assert response.status_code == 404

    async def test_an_account_with_no_mfa_is_still_204(self, client, admin, roleless):
        response = await client.delete(
            f"/users/{roleless.user_id}/mfa", headers=admin.headers
        )
        assert response.status_code == 204


class TestUserQueries:
    async def test_lookup_by_username_is_case_sensitive(self, admin):
        """The guest bug was a casing mismatch; pin the behaviour it relied on."""
        async with DatabaseService.session() as db:
            assert await User.get_user_by_username(db, admin.username) is not None
            assert await User.get_user_by_username(db, admin.username.upper()) is None

    async def test_lookup_by_missing_id(self):
        async with DatabaseService.session() as db:
            assert await User.get_user_by_id(db, 10_000) is None

    async def test_any_with_role(self, admin):
        async with DatabaseService.session() as db:
            assert await User.any_with_role(db, Roles.ADMIN.value) is True
            assert await User.any_with_role(db, Roles.MEMBER.value) is False

    async def test_repr_is_useful(self, admin):
        async with DatabaseService.session() as db:
            user = await User.get_user_by_id(db, admin.user_id)
        assert admin.username in repr(user)
