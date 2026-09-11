"""Document reads and writes: ownership, typing, the public/private split,
revision options, and deletion."""

import json
from typing import ClassVar

import pytest
from pydantic import ValidationError

from persistence.document import Document
from persistence.user import User
from services.auth.scopes import Scopes
from services.database.database_service import DatabaseService
from services.document.dtos.resume_object import PublicProjection, ResumePrivate


def latest(response) -> dict:
    """The single element the private-read envelope holds by default."""
    return response.json()["data"][0]


async def narrowed(client, actor, *scopes: Scopes) -> dict[str, str]:
    """Auth headers for a key of `actor`'s holding exactly `scopes`.

    A narrowed credential is how a caller is held below its owner now that
    both roles grant every document scope — there is no longer a role that
    reads but cannot write, because that distinction belongs on the key.
    """
    created = await client.post(
        "/api-keys",
        headers=actor.headers,
        json={"name": "narrowed", "scopes": [scope.value for scope in scopes]},
    )
    assert created.status_code == 200, created.text
    return {"Authorization": f"Bearer {created.json()['key']}"}


async def test_public_read_needs_no_credential(client, stored_resume):
    response = await client.get("/public/admin-user/resume/resume.json")
    assert response.status_code == 200
    assert response.json()["name"] == "resume.json"


async def test_public_read_by_id(client, admin, stored_resume):
    response = await client.get(f"/public/users/{admin.user_id}/resume/resume.json")
    assert response.status_code == 200
    assert response.json()["name"] == "resume.json"


async def test_public_read_omits_every_private_field(
    client, stored_resume, private_markers
):
    """`fineTuningData`, not `fine_tuning_data`: the route serializes the
    resume payload by alias, so the snake_case spelling is one this response
    can never carry and asserting on it would check nothing."""
    body = json.dumps(
        (await client.get("/public/admin-user/resume/resume.json")).json()
    )
    for marker in private_markers:
        assert marker not in body
    assert "fineTuningData" not in body


async def test_public_read_keeps_the_public_fields(client, stored_resume):
    data = (await client.get("/public/admin-user/resume/resume.json")).json()["data"]
    assert data["basics"]["name"] == "Test"
    assert data["work"][0]["highlights"][0]["summary"] == "did a thing"
    assert "email" not in data["basics"]
    assert "phone" not in data["basics"]


async def test_public_read_is_deny_by_default(client, stored_metadata):
    # Exists, but was never marked public — CreateDocumentRequest.public
    # defaults False.
    response = await client.get("/public/admin-user/resume/resume.metadata.json")
    assert response.status_code == 404


async def test_public_read_of_unknown_document(client, admin):
    response = await client.get("/public/admin-user/resume/nope.json")
    assert response.status_code == 404


async def test_public_read_of_unknown_user(client):
    assert (await client.get("/public/nobody/resume/resume.json")).status_code == 404


async def test_public_read_when_document_missing(client, admin):
    """The user exists, but nothing is stored yet."""
    response = await client.get("/public/admin-user/resume/resume.json")
    assert response.status_code == 404


async def test_public_read_404s_for_an_inactive_owner(client, admin, stored_resume):
    async with DatabaseService.session() as db:
        user = await User.get_user_by_id(db, admin.user_id)
        assert user is not None
        user.active = False
        await db.commit()

    response = await client.get("/public/admin-user/resume/resume.json")
    assert response.status_code == 404


async def test_private_read_requires_a_credential(client, stored_resume):
    assert (await client.get("/documents/resume/resume.json")).status_code == 401


async def test_private_read_requires_the_scope(client, stored_resume, roleless):
    response = await client.get(
        "/documents/resume/resume.json", headers=roleless.headers
    )
    assert response.status_code == 403
    assert "resume:read" in response.json()["detail"]


async def test_private_read_returns_the_list_envelope(client, stored_resume, admin):
    response = await client.get("/documents/resume/resume.json", headers=admin.headers)
    assert response.status_code == 200
    assert len(response.json()["data"]) == 1


async def test_private_read_returns_private_fields(
    client, stored_resume, admin, private_markers
):
    response = await client.get("/documents/resume/resume.json", headers=admin.headers)
    assert response.status_code == 200
    body = json.dumps(response.json())
    for marker in private_markers:
        assert marker in body


async def test_private_read_of_unknown_document(client, admin):
    response = await client.get("/documents/resume/missing.json", headers=admin.headers)
    assert response.status_code == 404


async def test_private_read_carries_type_public_and_created_at(
    client, stored_resume, admin
):
    response = await client.get("/documents/resume/resume.json", headers=admin.headers)
    entry = latest(response)
    assert entry["type"] == "resume"
    assert entry["public"] is True
    assert entry["created_at"] is not None


async def test_write_requires_the_scope(client, member, resume_payload):
    headers = await narrowed(client, member, Scopes.RESUME_READ)
    response = await client.post(
        "/documents/resume",
        headers=headers,
        json={"name": "resume.json", "revision_note": "v", "data": resume_payload},
    )
    assert response.status_code == 403
    assert Scopes.RESUME_WRITE.value in response.json()["detail"]


async def test_resume_write_does_not_grant_metadata_write(
    client, member, metadata_payload
):
    """The split is the point: one type's scope is not another's."""
    headers = await narrowed(client, member, Scopes.RESUME_WRITE)
    response = await client.post(
        "/documents/metadata",
        headers=headers,
        json={"name": "m.json", "revision_note": "v", "data": metadata_payload},
    )
    assert response.status_code == 403
    assert Scopes.METADATA_WRITE.value in response.json()["detail"]


async def test_resume_read_does_not_grant_skill_read(client, member, skill_payload):
    await client.post(
        "/documents/skill",
        headers=member.headers,
        json={"name": "s.json", "revision_note": "v", "data": skill_payload},
    )
    headers = await narrowed(client, member, Scopes.RESUME_READ)
    response = await client.get("/documents/skill/s.json", headers=headers)
    assert response.status_code == 403
    assert Scopes.SKILL_READ.value in response.json()["detail"]


async def test_write_requires_a_credential(client, resume_payload):
    response = await client.post(
        "/documents/resume",
        json={"name": "resume.json", "revision_note": "v", "data": resume_payload},
    )
    assert response.status_code == 401


async def test_write_starts_at_revision_one(stored_resume):
    assert stored_resume["revision_id"] == 1


async def test_write_increments_revision_when_data_changes(
    client, admin, stored_resume, resume_payload
):
    changed = {
        **resume_payload,
        "basics": {**resume_payload["basics"], "summary": "a different summary"},
    }
    response = await client.post(
        "/documents/resume",
        headers=admin.headers,
        json={
            "name": "resume.json",
            "revision_note": "v2",
            "public": True,
            "data": changed,
        },
    )
    assert response.json()["revision_id"] == stored_resume["revision_id"] + 1


async def test_write_is_a_no_op_when_data_and_public_are_unchanged(
    client, admin, stored_resume, resume_payload
):
    response = await client.post(
        "/documents/resume",
        headers=admin.headers,
        json={
            "name": "resume.json",
            "revision_note": "v2",
            "public": True,
            "data": resume_payload,
        },
    )
    assert response.json()["revision_id"] == stored_resume["revision_id"]


async def test_read_returns_the_latest_revision(
    client, admin, stored_resume, resume_payload
):
    changed = {
        **resume_payload,
        "basics": {**resume_payload["basics"], "summary": "newest"},
    }
    await client.post(
        "/documents/resume",
        headers=admin.headers,
        json={
            "name": "resume.json",
            "revision_note": "v2",
            "public": True,
            "data": changed,
        },
    )
    response = await client.get("/documents/resume/resume.json", headers=admin.headers)
    assert latest(response)["data"]["basics"]["summary"] == "newest"


async def test_camel_case_resume_body_is_accepted(client, admin, resume_payload):
    """Unlike every other document type, the resume payload is JSON Resume
    shaped and accepts camelCase on the way in — see `resume_object.Base`."""
    camel = {**resume_payload}
    camel["fineTuningData"] = {"narrative": {"careerArc": "arc"}}
    response = await client.post(
        "/documents/resume",
        headers=admin.headers,
        json={"name": "resume.json", "revision_note": "v", "data": camel},
    )
    assert response.status_code == 200, response.text


async def test_camel_case_metadata_body_is_still_rejected(
    client, admin, metadata_payload
):
    """The resume payload's camelCase acceptance is deliberately narrow — the
    metadata and skill documents keep the API's usual snake_case-only rule."""
    camel = {**metadata_payload}
    camel["schemaVersion"] = camel.pop("schema_version")
    response = await client.post(
        "/documents/metadata",
        headers=admin.headers,
        json={"name": "m.json", "revision_note": "v", "data": camel},
    )
    assert response.status_code == 422


async def test_resume_responses_are_camel_case(client, stored_resume, admin):
    """Unlike every other body in the API — see `resume_object.Base`."""
    private = json.dumps(
        (
            await client.get("/documents/resume/resume.json", headers=admin.headers)
        ).json()
    )
    assert "startDate" in private
    assert "start_date" not in private


async def test_metadata_responses_are_snake_case(client, stored_metadata, admin):
    private = json.dumps(
        (
            await client.get(
                "/documents/metadata/resume.metadata.json", headers=admin.headers
            )
        ).json()
    )
    assert "never_publish" in private
    assert "neverPublish" not in private


class TestUpsertStatus:
    """CreateDocumentResponse.status — whether a write produced a new
    revision or matched the current one exactly and wrote nothing."""

    async def test_first_write_is_created(self, client, admin, resume_payload):
        response = await client.post(
            "/documents/resume",
            headers=admin.headers,
            json={"name": "resume.json", "revision_note": "v1", "data": resume_payload},
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "created"
        assert response.json()["revision_id"] == 1

    async def test_identical_rewrite_is_unchanged(
        self, client, admin, stored_resume, resume_payload
    ):
        response = await client.post(
            "/documents/resume",
            headers=admin.headers,
            json={
                "name": "resume.json",
                "revision_note": "same content, different note",
                "public": True,
                "data": resume_payload,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "unchanged"
        assert response.json()["revision_id"] == stored_resume["revision_id"]

    async def test_changing_only_public_is_created(
        self, client, admin, stored_resume, resume_payload
    ):
        response = await client.post(
            "/documents/resume",
            headers=admin.headers,
            json={
                "name": "resume.json",
                "revision_note": "flip public",
                "public": False,
                "data": resume_payload,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "created"
        assert response.json()["revision_id"] == stored_resume["revision_id"] + 1

    async def test_changing_only_revision_note_is_unchanged(
        self, client, admin, stored_resume, resume_payload
    ):
        response = await client.post(
            "/documents/resume",
            headers=admin.headers,
            json={
                "name": "resume.json",
                "revision_note": "a brand new note, same everything else",
                "public": True,
                "data": resume_payload,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "unchanged"

    async def test_unchanged_write_does_not_record_the_new_note(
        self, client, admin, stored_resume, resume_payload
    ):
        await client.post(
            "/documents/resume",
            headers=admin.headers,
            json={
                "name": "resume.json",
                "revision_note": "this note should not stick",
                "public": True,
                "data": resume_payload,
            },
        )
        response = await client.get(
            "/documents/resume/resume.json", headers=admin.headers
        )
        assert latest(response)["revision_note"] != "this note should not stick"


class TestSkillDocument:
    """The instructions are a document like any other: same scopes, same
    append-only history, its own shape."""

    async def test_read_returns_the_stored_skill(self, client, admin, stored_skill):
        response = await client.get(
            "/documents/skill/resume.skill.json", headers=admin.headers
        )
        assert response.status_code == 200
        assert latest(response)["data"]["persona"] == "recruiter"

    async def test_read_requires_the_private_scope(
        self, client, roleless, stored_skill
    ):
        response = await client.get(
            "/documents/skill/resume.skill.json", headers=roleless.headers
        )
        assert response.status_code == 403

    async def test_write_requires_the_write_scope(self, client, member, skill_payload):
        headers = await narrowed(client, member, Scopes.SKILL_READ)
        response = await client.post(
            "/documents/skill",
            headers=headers,
            json={
                "name": "resume.skill.json",
                "revision_note": "v",
                "data": skill_payload,
            },
        )
        assert response.status_code == 403
        assert Scopes.SKILL_WRITE.value in response.json()["detail"]

    async def test_a_resume_is_not_a_skill(self, client, admin, resume_payload):
        """The shape is validated on the way in, so the skill route cannot
        store whatever happens to be posted to it."""
        response = await client.post(
            "/documents/skill",
            headers=admin.headers,
            json={
                "name": "resume.skill.json",
                "revision_note": "v",
                "data": resume_payload,
            },
        )
        assert response.status_code == 422

    async def test_an_unknown_severity_is_rejected(self, client, admin, skill_payload):
        """The guardrail severities are a closed set; a typo would otherwise be
        stored as an instruction nobody follows."""
        broken = {**skill_payload}
        broken["guardrails"] = [{"id": "g", "severity": "sometimes", "rule": "r"}]
        response = await client.post(
            "/documents/skill",
            headers=admin.headers,
            json={"name": "resume.skill.json", "revision_note": "v", "data": broken},
        )
        assert response.status_code == 422

    async def test_it_is_not_publicly_readable(self, client, admin, stored_skill):
        response = await client.get("/public/admin-user/resume/resume.skill.json")
        assert response.status_code == 404

    async def test_a_public_skill_is_still_not_served_by_the_public_resume_route(
        self, client, admin, skill_payload
    ):
        """The public route pins the type as well as the owner: marking a
        skill document public does not make it a resume."""
        await client.post(
            "/documents/skill",
            headers=admin.headers,
            json={
                "name": "resume.skill.json",
                "revision_note": "v",
                "public": True,
                "data": skill_payload,
            },
        )
        response = await client.get("/public/admin-user/resume/resume.skill.json")
        assert response.status_code == 404

    async def test_write_increments_revision_when_data_changes(
        self, client, admin, stored_skill, skill_payload
    ):
        changed = {**skill_payload, "objective": "something else"}
        response = await client.post(
            "/documents/skill",
            headers=admin.headers,
            json={"name": "resume.skill.json", "revision_note": "v2", "data": changed},
        )
        assert response.json()["revision_id"] == stored_skill["revision_id"] + 1

    def test_the_local_skill_document_still_validates(self):
        """docs/resume.skill.json is a local, gitignored file: the filled-in
        instructions that get posted to seed a deployment. Skipped where it does
        not exist, which is every fresh clone; where it does, it catches the
        model drifting away from the document before a post does."""
        from pathlib import Path

        from services.document.dtos.create_document import CreateDocumentRequest
        from services.document.dtos.resume_skill import ResumeSkill

        path = Path(__file__).resolve().parent.parent / "docs" / "resume.skill.json"
        if not path.exists():
            pytest.skip("no local skill document to check")

        request = CreateDocumentRequest[ResumeSkill].model_validate(
            json.loads(path.read_text())
        )
        assert request.name == "resume.skill.json"


class TestMetadataDocument:
    """Same treatment for the metadata, which until now could only be stored by
    posting it through the resume route."""

    async def test_a_resume_is_not_metadata(self, client, admin, resume_payload):
        response = await client.post(
            "/documents/metadata",
            headers=admin.headers,
            json={
                "name": "resume.metadata.json",
                "revision_note": "v",
                "data": resume_payload,
            },
        )
        assert response.status_code == 422

    async def test_round_trips(self, client, admin, stored_metadata):
        read = await client.get(
            "/documents/metadata/resume.metadata.json", headers=admin.headers
        )
        assert read.status_code == 200
        assert latest(read)["data"]["describes"] == "ResumePrivate"


class TestTypeIsPinnedToRoutes:
    """A document's type is fixed at creation; a route pins the type it
    accepts, not the name — names are free-form now."""

    async def test_a_skill_cannot_be_written_over_a_resume(
        self, client, admin, stored_resume, skill_payload
    ):
        response = await client.post(
            "/documents/skill",
            headers=admin.headers,
            json={"name": "resume.json", "revision_note": "v", "data": skill_payload},
        )
        assert response.status_code == 409
        assert "resume" in response.json()["detail"]

        # The resume is untouched, and still reads as a resume.
        intact = await client.get(
            "/documents/resume/resume.json", headers=admin.headers
        )
        assert latest(intact)["data"]["basics"]["name"] == "Test"

    async def test_the_public_route_cannot_be_poisoned(
        self, client, admin, stored_resume, skill_payload
    ):
        """The failure this prevents: the public feed serving, or 500ing on,
        something that is not a resume. The type conflict refuses the write
        outright, so the original resume is what keeps being served."""
        await client.post(
            "/documents/skill",
            headers=admin.headers,
            json={"name": "resume.json", "revision_note": "v", "data": skill_payload},
        )
        response = await client.get("/public/admin-user/resume/resume.json")
        assert response.status_code == 200
        assert response.json()["data"]["basics"]["name"] == "Test"

    async def test_metadata_cannot_be_written_as_a_resume(
        self, client, admin, stored_metadata, resume_payload
    ):
        response = await client.post(
            "/documents/resume",
            headers=admin.headers,
            json={
                "name": "resume.metadata.json",
                "revision_note": "m",
                "data": resume_payload,
            },
        )
        assert response.status_code == 409

    async def test_first_write_under_a_name_sets_its_type(
        self, client, admin, skill_payload
    ):
        """No conflict on the very first write — there is nothing to conflict
        with yet."""
        response = await client.post(
            "/documents/skill",
            headers=admin.headers,
            json={
                "name": "anything.json",
                "revision_note": "v",
                "data": skill_payload,
            },
        )
        assert response.status_code == 200

    async def test_reads_are_pinned_too(
        self, client, admin, stored_resume, stored_skill
    ):
        """Reading the resume through the skill route would validate it
        against the wrong model; under that route the name is not a document
        of that type."""
        response = await client.get(
            "/documents/skill/resume.json", headers=admin.headers
        )
        assert response.status_code == 404

    async def test_the_scope_check_still_comes_first(
        self, client, roleless, stored_resume, skill_payload
    ):
        """A caller who may not write learns nothing about a type conflict."""
        response = await client.post(
            "/documents/skill",
            headers=roleless.headers,
            json={"name": "resume.json", "revision_note": "v", "data": skill_payload},
        )
        assert response.status_code == 403


class TestOwnershipIsolation:
    async def test_two_users_can_each_hold_a_document_of_the_same_name(
        self, client, admin, other_owner, stored_resume, resume_payload
    ):
        other_payload = {
            **resume_payload,
            "basics": {
                **resume_payload["basics"],
                "summary": "the other owner's summary",
            },
        }
        await client.post(
            "/documents/resume",
            headers=other_owner.headers,
            json={
                "name": "resume.json",
                "revision_note": "v",
                "public": True,
                "data": other_payload,
            },
        )

        mine = await client.get("/documents/resume/resume.json", headers=admin.headers)
        theirs = await client.get(
            "/documents/resume/resume.json", headers=other_owner.headers
        )
        assert latest(mine)["data"]["basics"]["summary"] == "summary"
        assert (
            latest(theirs)["data"]["basics"]["summary"] == "the other owner's summary"
        )

    async def test_public_routes_are_scoped_by_owner_too(
        self, client, admin, other_owner, resume_payload, stored_resume
    ):
        other_payload = {
            **resume_payload,
            "basics": {
                **resume_payload["basics"],
                "summary": "the other owner's summary",
            },
        }
        await client.post(
            "/documents/resume",
            headers=other_owner.headers,
            json={
                "name": "resume.json",
                "revision_note": "v",
                "public": True,
                "data": other_payload,
            },
        )

        mine = await client.get("/public/admin-user/resume/resume.json")
        theirs = await client.get("/public/other-owner/resume/resume.json")
        assert mine.json()["data"]["basics"]["summary"] == "summary"
        assert theirs.json()["data"]["basics"]["summary"] == "the other owner's summary"

    async def test_deleting_your_own_does_not_touch_theirs(
        self, client, admin, other_owner, stored_resume, resume_payload
    ):
        await client.post(
            "/documents/resume",
            headers=other_owner.headers,
            json={"name": "resume.json", "revision_note": "v", "data": resume_payload},
        )

        await client.delete("/documents/resume.json", headers=admin.headers)

        gone = await client.get("/documents/resume/resume.json", headers=admin.headers)
        still_there = await client.get(
            "/documents/resume/resume.json", headers=other_owner.headers
        )
        assert gone.status_code == 404
        assert still_there.status_code == 200


class TestPublicFlag:
    async def test_flipping_public_off_hides_it(
        self, client, admin, stored_resume, resume_payload
    ):
        response = await client.post(
            "/documents/resume",
            headers=admin.headers,
            json={
                "name": "resume.json",
                "revision_note": "make it private",
                "public": False,
                "data": resume_payload,
            },
        )
        assert response.json()["revision_id"] == stored_resume["revision_id"] + 1

        hidden = await client.get("/public/admin-user/resume/resume.json")
        assert hidden.status_code == 404

    async def test_flipping_it_back_serves_it_again_as_a_new_revision(
        self, client, admin, stored_resume, resume_payload
    ):
        off = await client.post(
            "/documents/resume",
            headers=admin.headers,
            json={
                "name": "resume.json",
                "revision_note": "off",
                "public": False,
                "data": resume_payload,
            },
        )
        assert (
            await client.get("/public/admin-user/resume/resume.json")
        ).status_code == 404

        on = await client.post(
            "/documents/resume",
            headers=admin.headers,
            json={
                "name": "resume.json",
                "revision_note": "on",
                "public": True,
                "data": resume_payload,
            },
        )
        # A new revision each time, even though `data` never changed.
        assert off.json()["revision_id"] == stored_resume["revision_id"] + 1
        assert on.json()["revision_id"] == off.json()["revision_id"] + 1

        restored = await client.get("/public/admin-user/resume/resume.json")
        assert restored.status_code == 200

    async def test_omitting_public_leaves_the_flag_alone(
        self, client, admin, stored_resume, resume_payload
    ):
        """The failure this guards against is quiet: a write that only edits
        content, with no `public` in the body, taking the published document
        offline. `public` is three-valued so that unstated means unchanged."""
        revised = {
            **resume_payload,
            "basics": {**resume_payload["basics"], "summary": "reworded"},
        }
        response = await client.post(
            "/documents/resume",
            headers=admin.headers,
            json={"name": "resume.json", "revision_note": "edit", "data": revised},
        )
        assert response.json()["revision_id"] == stored_resume["revision_id"] + 1

        still_served = await client.get("/public/admin-user/resume/resume.json")
        assert still_served.status_code == 200
        assert still_served.json()["public"] is True

    async def test_omitting_public_leaves_a_private_document_private(
        self, client, admin, stored_skill, skill_payload
    ):
        """The same rule in the other direction: unstated does not mean
        `false` either, it means whatever the document already said."""
        revised = {**skill_payload, "objective": "reworded"}
        await client.post(
            "/documents/skill",
            headers=admin.headers,
            json={"name": "resume.skill.json", "revision_note": "e", "data": revised},
        )
        response = await client.get(
            "/documents/skill/resume.skill.json", headers=admin.headers
        )
        assert response.json()["data"][0]["public"] is False

    async def test_a_first_write_that_omits_public_defaults_closed(
        self, client, admin, resume_payload
    ):
        """No current revision to inherit from, so deny-by-default applies."""
        await client.post(
            "/documents/resume",
            headers=admin.headers,
            json={"name": "second.json", "revision_note": "n", "data": resume_payload},
        )
        response = await client.get("/public/admin-user/resume/second.json")
        assert response.status_code == 404


class TestEntryWithholding:
    """`publish: false` on one entry, exercised through the real routes rather
    than PublicProjection directly — the record-level counterpart to
    TestPublicFlag's whole-document flag."""

    async def test_a_withheld_job_is_absent_from_the_public_feed(
        self, client, admin, withheld_resume_payload
    ):
        await client.post(
            "/documents/resume",
            headers=admin.headers,
            json={
                "name": "resume.json",
                "revision_note": "initial",
                "public": True,
                "data": withheld_resume_payload,
            },
        )
        response = await client.get("/public/admin-user/resume/resume.json")
        assert response.status_code == 200
        companies = [entry["name"] for entry in response.json()["data"]["work"]]
        assert "Hidden Co" not in companies

    async def test_a_withheld_job_is_present_in_the_private_read(
        self, client, admin, withheld_resume_payload
    ):
        await client.post(
            "/documents/resume",
            headers=admin.headers,
            json={
                "name": "resume.json",
                "revision_note": "initial",
                "public": True,
                "data": withheld_resume_payload,
            },
        )
        response = await client.get(
            "/documents/resume/resume.json", headers=admin.headers
        )
        companies = [entry["name"] for entry in latest(response)["data"]["work"]]
        assert "Hidden Co" in companies


class TestDocumentNames:
    """A name is a URL path segment and a document's only handle. One that
    cannot be spelled as a path segment stored a document that could then be
    neither read, nor deleted, nor served."""

    @pytest.mark.parametrize(
        "name", ["", "   ", "a/b", "..\\b", "with\nnewline", "x" * 256]
    )
    async def test_unreachable_names_are_refused(
        self, client, admin, resume_payload, name
    ):
        response = await client.post(
            "/documents/resume",
            headers=admin.headers,
            json={"name": name, "revision_note": "n", "data": resume_payload},
        )
        assert response.status_code == 422, response.text

    async def test_an_ordinary_name_is_still_free_form(
        self, client, admin, resume_payload
    ):
        """The gate is on what breaks, not on a naming convention — the whole
        point of dropping DocumentName was that the operator picks the name."""
        response = await client.post(
            "/documents/resume",
            headers=admin.headers,
            json={
                "name": "résumé (2026 draft).json",
                "revision_note": "n",
                "data": resume_payload,
            },
        )
        assert response.status_code == 200, response.text
        read_back = await client.get(
            "/documents/resume/résumé (2026 draft).json", headers=admin.headers
        )
        assert read_back.status_code == 200


class TestRevisionOptions:
    async def test_default_is_one_revision_newest_first(
        self, client, admin, stored_resume, resume_payload
    ):
        for i in range(3):
            await client.post(
                "/documents/resume",
                headers=admin.headers,
                json={
                    "name": "resume.json",
                    "revision_note": f"v{i}",
                    "public": True,
                    "data": {
                        **resume_payload,
                        "basics": {
                            **resume_payload["basics"],
                            "summary": f"summary-{i}",
                        },
                    },
                },
            )
        response = await client.get(
            "/documents/resume/resume.json", headers=admin.headers
        )
        data = response.json()["data"]
        assert len(data) == 1
        assert data[0]["data"]["basics"]["summary"] == "summary-2"

    async def test_revisions_and_oldest_first_selects_the_most_recent_slice(
        self, client, admin, stored_resume, resume_payload
    ):
        """revisions=3&order=oldest_first is the three MOST RECENT revisions,
        oldest of those three first — not the three oldest revisions."""
        for i in range(4):
            await client.post(
                "/documents/resume",
                headers=admin.headers,
                json={
                    "name": "resume.json",
                    "revision_note": f"v{i}",
                    "public": True,
                    "data": {
                        **resume_payload,
                        "basics": {
                            **resume_payload["basics"],
                            "summary": f"summary-{i}",
                        },
                    },
                },
            )
        # Revisions on disk: initial(1), v0(2), v1(3), v2(4), v3(5).
        response = await client.get(
            "/documents/resume/resume.json?revisions=3&order=oldest_first",
            headers=admin.headers,
        )
        data = response.json()["data"]
        assert [d["data"]["basics"]["summary"] for d in data] == [
            "summary-1",
            "summary-2",
            "summary-3",
        ]
        assert [d["revision_id"] for d in data] == [3, 4, 5]

    async def test_revisions_newest_first(
        self, client, admin, stored_resume, resume_payload
    ):
        for i in range(2):
            await client.post(
                "/documents/resume",
                headers=admin.headers,
                json={
                    "name": "resume.json",
                    "revision_note": f"v{i}",
                    "public": True,
                    "data": {
                        **resume_payload,
                        "basics": {
                            **resume_payload["basics"],
                            "summary": f"summary-{i}",
                        },
                    },
                },
            )
        response = await client.get(
            "/documents/resume/resume.json?revisions=2&order=newest_first",
            headers=admin.headers,
        )
        data = response.json()["data"]
        assert [d["data"]["basics"]["summary"] for d in data] == [
            "summary-1",
            "summary-0",
        ]

    async def test_revisions_over_the_cap_is_rejected(
        self, client, admin, stored_resume
    ):
        response = await client.get(
            "/documents/resume/resume.json?revisions=51", headers=admin.headers
        )
        assert response.status_code == 422

    async def test_revisions_of_zero_is_rejected(self, client, admin, stored_resume):
        response = await client.get(
            "/documents/resume/resume.json?revisions=0", headers=admin.headers
        )
        assert response.status_code == 422


class TestMixedSchemaHistory:
    """The bug SCHEMA_VERSIONING_PLAN.md Phase 6 fixes: validating every
    revision against the *current* model 500s the moment a document's
    history spans a schema change — which Phase 4/5's conversions make the
    normal shape of a migrated account's history."""

    async def test_a_v1_shaped_revision_no_longer_500s(
        self, client, admin, stored_resume
    ):
        v1_shaped = {"profile": {"name": "old shape"}, "no": "basics key here"}
        async with DatabaseService.session() as db:
            db.add(
                Document(
                    created_by=admin.user_id,
                    name="resume.json",
                    revision_id=0,
                    type="resume",
                    public=True,
                    revision_note="pre-migration",
                    data=v1_shaped,
                    schema_version=1,
                )
            )
            await db.commit()

        response = await client.get(
            "/documents/resume/resume.json?revisions=2&order=oldest_first",
            headers=admin.headers,
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert len(data) == 2
        # The v1 revision comes back exactly as stored, unvalidated.
        assert data[0]["revision_id"] == 0
        assert data[0]["data"] == v1_shaped
        # The current revision still validates normally.
        assert data[1]["revision_id"] == 1
        assert "basics" in data[1]["data"]


class TestListDocuments:
    async def test_lists_the_latest_revision_of_each_document(
        self, client, admin, stored_resume, stored_metadata, stored_skill
    ):
        response = await client.get("/documents", headers=admin.headers)
        assert response.status_code == 200
        names = {entry["name"] for entry in response.json()["data"]}
        assert names == {"resume.json", "resume.metadata.json", "resume.skill.json"}

    async def test_three_revisions_of_one_document_is_one_row(
        self, client, admin, stored_resume, resume_payload
    ):
        for i in range(2):
            await client.post(
                "/documents/resume",
                headers=admin.headers,
                json={
                    "name": "resume.json",
                    "revision_note": f"v{i}",
                    "public": True,
                    "data": {
                        **resume_payload,
                        "basics": {
                            **resume_payload["basics"],
                            "summary": f"summary-{i}",
                        },
                    },
                },
            )
        response = await client.get("/documents", headers=admin.headers)
        rows = [row for row in response.json()["data"] if row["name"] == "resume.json"]
        assert len(rows) == 1
        assert rows[0]["revision_id"] == 3

    async def test_scoped_to_the_caller(
        self, client, admin, other_owner, stored_resume, resume_payload
    ):
        await client.post(
            "/documents/resume",
            headers=other_owner.headers,
            json={"name": "resume.json", "revision_note": "v", "data": resume_payload},
        )
        response = await client.get("/documents", headers=other_owner.headers)
        assert len(response.json()["data"]) == 1

    async def test_lists_only_the_types_the_credential_may_read(
        self, client, admin, stored_resume, stored_metadata, stored_skill
    ):
        """A key narrowed to one type sees that type. It is the answer that
        credential should get, rather than a refusal for the two it was
        deliberately not given."""
        headers = await narrowed(client, admin, Scopes.SKILL_READ)
        response = await client.get("/documents", headers=headers)
        assert response.status_code == 200
        assert {row["type"] for row in response.json()["data"]} == {"skill"}

    async def test_refused_only_when_no_type_is_readable(
        self, client, admin, stored_resume
    ):
        headers = await narrowed(client, admin, Scopes.RESUME_WRITE)
        response = await client.get("/documents", headers=headers)
        assert response.status_code == 403
        # Names all three, because any one of them would have done.
        for scope in (Scopes.RESUME_READ, Scopes.METADATA_READ, Scopes.SKILL_READ):
            assert scope.value in response.json()["detail"]

    async def test_requires_some_read_scope(self, client, roleless):
        response = await client.get("/documents", headers=roleless.headers)
        assert response.status_code == 403

    async def test_requires_a_credential(self, client):
        assert (await client.get("/documents")).status_code == 401


class TestDocumentSchemas:
    async def test_returns_all_three_for_a_full_credential(self, client, admin):
        response = await client.get("/documents/schemas", headers=admin.headers)
        assert response.status_code == 200
        assert set(response.json()["schemas"]) == {"resume", "metadata", "skill"}

    async def test_narrowed_to_one_type_sees_one_entry(self, client, admin):
        headers = await narrowed(client, admin, Scopes.SKILL_READ)
        response = await client.get("/documents/schemas", headers=headers)
        assert response.status_code == 200
        assert set(response.json()["schemas"]) == {"skill"}

    async def test_no_read_scope_is_403(self, client, admin):
        headers = await narrowed(client, admin, Scopes.RESUME_WRITE)
        response = await client.get("/documents/schemas", headers=headers)
        assert response.status_code == 403

    async def test_anonymous_is_401(self, client):
        assert (await client.get("/documents/schemas")).status_code == 401

    async def test_forbids_unknown_fields(self, client, admin, metadata_payload):
        """extra="forbid" on the models means additionalProperties: false on
        the schema — the point of sourcing it from the server rather than
        hand-maintaining one."""
        import jsonschema

        response = await client.get("/documents/schemas", headers=admin.headers)
        schema = response.json()["schemas"]["metadata"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**metadata_payload, "unknown_field": True}, schema)

    async def test_schema_validates_a_known_good_stored_document(
        self, client, admin, resume_payload
    ):
        import jsonschema

        response = await client.get("/documents/schemas", headers=admin.headers)
        schema = response.json()["schemas"]["resume"]
        jsonschema.validate(resume_payload, schema)

    async def test_publish_field_carries_its_agent_facing_description(
        self, client, admin
    ):
        """This pins the one place the withholding-vs-tailoring distinction
        is guaranteed to reach a client: the schema itself, which survives a
        fork that never edits its metadata document."""
        response = await client.get("/documents/schemas", headers=admin.headers)
        schema = response.json()["schemas"]["resume"]
        work = schema["$defs"]["Work"]["properties"]["publish"]
        assert work["default"] is True
        assert "public feed only" in work["description"]
        assert "tailored resume" in work["description"]


class TestDeletion:
    async def test_deleting_removes_every_revision(
        self, client, admin, stored_resume, resume_payload
    ):
        await client.post(
            "/documents/resume",
            headers=admin.headers,
            json={
                "name": "resume.json",
                "revision_note": "v2",
                "data": {
                    **resume_payload,
                    "basics": {**resume_payload["basics"], "summary": "changed"},
                },
            },
        )

        response = await client.delete("/documents/resume.json", headers=admin.headers)
        assert response.status_code == 200
        assert response.json() == {"name": "resume.json", "revisions_deleted": 2}

        gone = await client.get("/documents/resume/resume.json", headers=admin.headers)
        assert gone.status_code == 404

    async def test_deleting_an_unknown_document_404s(self, client, admin):
        response = await client.delete("/documents/nope.json", headers=admin.headers)
        assert response.status_code == 404

    async def test_deleting_someone_elses_document_404s_and_leaves_it_intact(
        self, client, admin, other_owner, stored_resume
    ):
        response = await client.delete(
            "/documents/resume.json", headers=other_owner.headers
        )
        assert response.status_code == 404

        still_there = await client.get(
            "/documents/resume/resume.json", headers=admin.headers
        )
        assert still_there.status_code == 200

    async def test_requires_the_delete_scope(self, client, admin, stored_resume):
        created = await client.post(
            "/api-keys",
            headers=admin.headers,
            json={
                "name": "writer",
                "scopes": [Scopes.RESUME_WRITE.value, Scopes.RESUME_READ.value],
            },
        )
        key = created.json()["key"]

        response = await client.delete(
            "/documents/resume.json",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert response.status_code == 403

        # The owner's own JWT still works.
        response = await client.delete("/documents/resume.json", headers=admin.headers)
        assert response.status_code == 200

    async def test_requires_a_credential(self, client, stored_resume):
        response = await client.delete("/documents/resume.json")
        assert response.status_code == 401

    async def test_the_delete_scope_is_per_type(self, client, admin, stored_skill):
        """The route names only a document, so the type is discovered by
        loading it — a key that may delete résumés may not delete this."""
        headers = await narrowed(client, admin, Scopes.RESUME_DELETE)
        response = await client.delete("/documents/resume.skill.json", headers=headers)
        assert response.status_code == 403
        assert Scopes.SKILL_DELETE.value in response.json()["detail"]

    async def test_the_matching_type_scope_succeeds(self, client, admin, stored_skill):
        headers = await narrowed(client, admin, Scopes.SKILL_DELETE)
        response = await client.delete("/documents/resume.skill.json", headers=headers)
        assert response.status_code == 200, response.text

    async def test_an_unknown_name_404s_before_the_scope_check(
        self, client, admin, stored_resume
    ):
        """Which delete scope applies is not known until the document is
        loaded, so the refusal must not become an existence oracle: a name
        that is not there is a 404 whatever the caller holds."""
        headers = await narrowed(client, admin, Scopes.RESUME_READ)
        response = await client.delete("/documents/nope.json", headers=headers)
        assert response.status_code == 404

    async def test_deleting_a_referenced_document_clears_the_application(
        self, client, admin, company, stored_resume
    ):
        """See section 4.8 of the tracking plan: the composite FK has no ON
        DELETE clause, so the application's reference must be cleared by hand
        before the document goes, in the same transaction."""
        created = await client.post(
            "/applications",
            headers=admin.headers,
            json={
                "company_id": company["id"],
                "resume_document_name": stored_resume["name"],
                "resume_revision_id": stored_resume["revision_id"],
            },
        )
        assert created.status_code == 201, created.text
        application_id = created.json()["id"]

        response = await client.delete("/documents/resume.json", headers=admin.headers)
        assert response.status_code == 200

        detail = await client.get(
            f"/applications/{application_id}", headers=admin.headers
        )
        assert detail.status_code == 200
        assert detail.json()["resume_document"] is None
        # The document name survives as a human-readable label rather than
        # being silently lost.
        assert detail.json()["resume_label"] == "resume.json"


class TestRename:
    async def test_moves_every_revision(
        self, client, admin, stored_resume, resume_payload
    ):
        await client.post(
            "/documents/resume",
            headers=admin.headers,
            json={
                "name": "resume.json",
                "revision_note": "v2",
                "public": True,
                "data": {
                    **resume_payload,
                    "basics": {**resume_payload["basics"], "summary": "changed"},
                },
            },
        )

        response = await client.patch(
            "/documents/resume.json",
            headers=admin.headers,
            json={"name": "renamed.json"},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "old_name": "resume.json",
            "name": "renamed.json",
            "revisions_moved": 2,
        }

        moved = await client.get(
            "/documents/resume/renamed.json?revisions=50", headers=admin.headers
        )
        assert moved.status_code == 200
        revisions = moved.json()["data"]
        assert len(revisions) == 2
        assert {rev["revision_id"] for rev in revisions} == {1, 2}
        assert {rev["revision_note"] for rev in revisions} == {"initial", "v2"}

    async def test_old_name_is_gone(self, client, admin, stored_resume):
        await client.patch(
            "/documents/resume.json",
            headers=admin.headers,
            json={"name": "renamed.json"},
        )
        response = await client.get(
            "/documents/resume/resume.json", headers=admin.headers
        )
        assert response.status_code == 404

    async def test_rename_onto_an_existing_name_is_a_conflict(
        self, client, admin, stored_resume, stored_metadata
    ):
        response = await client.patch(
            "/documents/resume.json",
            headers=admin.headers,
            json={"name": "resume.metadata.json"},
        )
        assert response.status_code == 409

        # Nothing moved.
        still_there = await client.get(
            "/documents/resume/resume.json", headers=admin.headers
        )
        assert still_there.status_code == 200

    async def test_rename_to_the_current_name_says_so(
        self, client, admin, stored_resume
    ):
        """400, not the 409 the store would raise on its own: a document
        colliding with itself is a true but useless thing to be told."""
        response = await client.patch(
            "/documents/resume.json",
            headers=admin.headers,
            json={"name": "resume.json"},
        )
        assert response.status_code == 400
        assert "already has" in response.json()["detail"]

        still_there = await client.get(
            "/documents/resume/resume.json", headers=admin.headers
        )
        assert still_there.status_code == 200

    async def test_renaming_someone_elses_document_404s(
        self, client, admin, other_owner, stored_resume
    ):
        response = await client.patch(
            "/documents/resume.json",
            headers=other_owner.headers,
            json={"name": "renamed.json"},
        )
        assert response.status_code == 404

        still_there = await client.get(
            "/documents/resume/resume.json", headers=admin.headers
        )
        assert still_there.status_code == 200

    async def test_write_without_delete_is_403(self, client, admin, stored_resume):
        headers = await narrowed(client, admin, Scopes.RESUME_WRITE, Scopes.RESUME_READ)
        response = await client.patch(
            "/documents/resume.json",
            headers=headers,
            json={"name": "renamed.json"},
        )
        assert response.status_code == 403

    async def test_delete_without_write_is_403(self, client, admin, stored_resume):
        headers = await narrowed(
            client, admin, Scopes.RESUME_DELETE, Scopes.RESUME_READ
        )
        response = await client.patch(
            "/documents/resume.json",
            headers=headers,
            json={"name": "renamed.json"},
        )
        assert response.status_code == 403

    async def test_write_and_delete_together_succeed(
        self, client, admin, stored_resume
    ):
        headers = await narrowed(
            client, admin, Scopes.RESUME_WRITE, Scopes.RESUME_DELETE
        )
        response = await client.patch(
            "/documents/resume.json",
            headers=headers,
            json={"name": "renamed.json"},
        )
        assert response.status_code == 200, response.text

    async def test_an_unknown_name_404s(self, client, admin):
        response = await client.patch(
            "/documents/nope.json",
            headers=admin.headers,
            json={"name": "somewhere-else.json"},
        )
        assert response.status_code == 404

    @pytest.mark.parametrize(
        "bad_name", ["", "   ", "has/slash", "has\\backslash", "control\x00char"]
    )
    async def test_rejects_unreachable_new_names(
        self, client, admin, stored_resume, bad_name
    ):
        response = await client.patch(
            "/documents/resume.json",
            headers=admin.headers,
            json={"name": bad_name},
        )
        assert response.status_code == 422

    async def test_requires_a_credential(self, client, stored_resume):
        response = await client.patch(
            "/documents/resume.json", json={"name": "renamed.json"}
        )
        assert response.status_code == 401

    async def test_does_not_trip_the_append_only_guard(
        self, client, admin, stored_resume
    ):
        """A rename deletes and re-inserts rather than updating `name` in
        place — if it ever regressed to an UPDATE, block_mutations would raise
        PermissionError and this would 500 instead of 200."""
        response = await client.patch(
            "/documents/resume.json",
            headers=admin.headers,
            json={"name": "renamed.json"},
        )
        assert response.status_code == 200

    async def test_renaming_a_referenced_document_retargets_the_application(
        self, client, admin, company, stored_resume
    ):
        created = await client.post(
            "/applications",
            headers=admin.headers,
            json={
                "company_id": company["id"],
                "resume_document_name": stored_resume["name"],
                "resume_revision_id": stored_resume["revision_id"],
            },
        )
        assert created.status_code == 201, created.text
        application_id = created.json()["id"]

        response = await client.patch(
            "/documents/resume.json",
            headers=admin.headers,
            json={"name": "renamed.json"},
        )
        assert response.status_code == 200

        detail = await client.get(
            f"/applications/{application_id}", headers=admin.headers
        )
        assert detail.status_code == 200
        assert detail.json()["resume_document"] == {
            "name": "renamed.json",
            "revision_id": stored_resume["revision_id"],
        }


class TestPublicProjection:
    """PublicProjection is the redaction primitive; the routes depend on it
    holding. See `tests/test_public_projection.py` for the structural
    guarantees; this class covers the withholding behaviour."""

    def test_unflagged_input_projects_exactly_as_before(self, resume_payload):
        """The regression guard: `resume_payload` sets no `publish` anywhere,
        so withholding must be a no-op and the change purely additive."""
        public = PublicProjection.of(ResumePrivate.model_validate(resume_payload))
        work = resume_payload["work"][0]
        highlight = work["highlights"][0]
        skill = resume_payload["skills"][0]
        keyword = skill["keywords"][0]
        assert public.model_dump(exclude_defaults=True) == {
            "basics": {
                "name": resume_payload["basics"]["name"],
                "label": resume_payload["basics"]["label"],
                "tagline": resume_payload["basics"]["tagline"],
                "summary": resume_payload["basics"]["summary"],
                "location": {
                    "label": resume_payload["basics"]["location"]["label"],
                    "kind": resume_payload["basics"]["location"]["kind"],
                    "note": resume_payload["basics"]["location"]["note"],
                },
                "profiles": resume_payload["basics"]["profiles"],
            },
            "work": [
                {
                    "name": work["name"],
                    "location": work["location"],
                    "description": work["description"],
                    "position": work["position"],
                    "start_date": work["startDate"],
                    "role_location": work["roleLocation"],
                    "roles": [
                        {"title": "Engineer", "start_date": "2020"},
                    ],
                    "highlights": [
                        {
                            "id": highlight["id"],
                            "summary": highlight["summary"],
                            "specifics": [{"detail": "detail"}],
                        }
                    ],
                }
            ],
            "skills": [
                {
                    "name": skill["name"],
                    "keywords": [
                        {"name": keyword["name"], "url": keyword["url"]},
                    ],
                }
            ],
        }

    def test_drops_private_fields_at_every_depth(self, resume_payload, private_markers):
        public = PublicProjection.of(ResumePrivate.model_validate(resume_payload))
        body = json.dumps(public.model_dump())
        for marker in private_markers:
            assert marker not in body

    def test_keeps_public_fields(self, resume_payload):
        public = PublicProjection.of(ResumePrivate.model_validate(resume_payload))
        assert public.work[0].highlights[0].summary == "did a thing"
        assert public.basics.name == "Test"

    def test_drops_skill_ratings_but_keeps_the_skill(self, resume_payload):
        """`level` is a fixed vocabulary, so it cannot carry a unique marker
        into private_markers the way `lastUsed` does. Assert on the key."""
        public = PublicProjection.of(ResumePrivate.model_validate(resume_payload))
        keyword = public.skills[0].keywords[0].model_dump()
        assert keyword["name"] == "Python"
        assert "level" not in keyword
        assert "last_used" not in keyword

    def test_a_private_field_left_unstripped_raises_instead_of_serving(
        self, resume_payload
    ):
        """The structural guarantee this whole tree exists for: a field
        `NEVER_PUBLISHED` fails to exclude does not quietly serve, because
        `ResumePublic.model_validate` has no slot for it and `extra="forbid"`
        is still in force."""

        class BrokenProjection(PublicProjection):
            NEVER_PUBLISHED: ClassVar[dict] = {}  # pretend nothing is excluded now

        private = ResumePrivate.model_validate(resume_payload)
        with pytest.raises(ValidationError):
            BrokenProjection(private).build()

    def test_publish_flag_never_reaches_the_public_payload(self, resume_payload):
        """The flag is declared on the private classes only, so redaction
        drops it at every depth, with no explicit exclusion anywhere."""
        public = PublicProjection.of(ResumePrivate.model_validate(resume_payload))
        assert "publish" not in json.dumps(public.model_dump())

    def test_a_withheld_work_entry_is_dropped(self, resume_payload):
        withheld = {
            **resume_payload,
            "work": [{**resume_payload["work"][0], "publish": False}],
        }
        public = PublicProjection.of(ResumePrivate.model_validate(withheld))
        assert public.work == []

    def test_a_withheld_highlight_inside_a_published_work_entry_is_dropped(
        self, resume_payload
    ):
        work = resume_payload["work"][0]
        withheld = {
            **resume_payload,
            "work": [
                {
                    **work,
                    "highlights": [{**work["highlights"][0], "publish": False}],
                }
            ],
        }
        public = PublicProjection.of(ResumePrivate.model_validate(withheld))
        assert len(public.work) == 1
        assert public.work[0].highlights == []

    def test_a_published_work_entry_survives_every_highlight_withheld(
        self, resume_payload
    ):
        """The employment record is load-bearing: dropping it would leave a
        silent gap in the timeline, so only the highlights empty out."""
        work = resume_payload["work"][0]
        withheld = {
            **resume_payload,
            "work": [
                {
                    **work,
                    "highlights": [{**work["highlights"][0], "publish": False}],
                }
            ],
        }
        public = PublicProjection.of(ResumePrivate.model_validate(withheld))
        assert public.work[0].name == work["name"]

    def test_a_withheld_skill_group_is_dropped(self, resume_payload):
        withheld = {
            **resume_payload,
            "skills": [{**resume_payload["skills"][0], "publish": False}],
        }
        public = PublicProjection.of(ResumePrivate.model_validate(withheld))
        assert public.skills == []

    def test_a_withheld_keyword_is_dropped_but_the_group_survives(self, resume_payload):
        group = resume_payload["skills"][0]
        keywords = group["keywords"] + [{**group["keywords"][0], "name": "Rust"}]
        withheld = {
            **resume_payload,
            "skills": [
                {**group, "keywords": [{**keywords[0], "publish": False}, keywords[1]]}
            ],
        }
        public = PublicProjection.of(ResumePrivate.model_validate(withheld))
        assert len(public.skills) == 1
        assert [k.name for k in public.skills[0].keywords] == ["Rust"]

    def test_a_published_group_with_every_keyword_withheld_is_dropped_too(
        self, resume_payload
    ):
        """Unlike a work entry's highlights, a skill group with nothing left
        under it is dropped entirely — a heading with no skills is a
        rendering artifact, not information."""
        group = resume_payload["skills"][0]
        withheld = {
            **resume_payload,
            "skills": [
                {**group, "keywords": [{**group["keywords"][0], "publish": False}]}
            ],
        }
        public = PublicProjection.of(ResumePrivate.model_validate(withheld))
        assert public.skills == []

    def test_a_withheld_certificate_is_dropped(self, resume_payload):
        withheld = {
            **resume_payload,
            "certificates": [{"name": "Withheld Cert", "publish": False}],
        }
        public = PublicProjection.of(ResumePrivate.model_validate(withheld))
        assert public.certificates == []

    def test_a_withheld_education_entry_is_dropped(self, resume_payload):
        withheld = {
            **resume_payload,
            "education": [{"studyType": "Withheld Degree", "publish": False}],
        }
        public = PublicProjection.of(ResumePrivate.model_validate(withheld))
        assert public.education == []

    def test_a_withheld_project_is_dropped(self, resume_payload):
        withheld = {
            **resume_payload,
            "projects": [{"description": "Withheld Project", "publish": False}],
        }
        public = PublicProjection.of(ResumePrivate.model_validate(withheld))
        assert public.projects == []

    def test_every_location_withheld_keeps_basics_without_a_location(
        self, resume_payload
    ):
        """Nothing on the page requires a location to be present, so this is
        a plain filter with no special-casing — unlike a skill group, basics
        itself is never dropped."""
        basics = resume_payload["basics"]
        withheld = {
            **resume_payload,
            "basics": {**basics, "location": {**basics["location"], "publish": False}},
        }
        public = PublicProjection.of(ResumePrivate.model_validate(withheld))
        assert public.basics.location is None

    def test_every_profile_withheld_keeps_basics_with_an_empty_list(
        self, resume_payload
    ):
        basics = resume_payload["basics"]
        withheld = {
            **resume_payload,
            "basics": {
                **basics,
                "profiles": [{**basics["profiles"][0], "publish": False}],
            },
        }
        public = PublicProjection.of(ResumePrivate.model_validate(withheld))
        assert public.basics.profiles == []


class TestAppendOnlyDocuments:
    """The documents table forbids updates, in both the ORM and the database.
    Deletes are a supported operation now."""

    async def test_orm_blocks_updates(self, admin, stored_resume):
        async with DatabaseService.session() as db:
            document = await Document.get_latest(db, admin.user_id, "resume.json")
            assert document is not None
            document.revision_note = "tampered"
            with pytest.raises(PermissionError):
                await db.commit()

    async def test_orm_permits_deletes(self, admin, stored_resume):
        async with DatabaseService.session() as db:
            document = await Document.get_latest(db, admin.user_id, "resume.json")
            assert document is not None
            await db.delete(document)
            await db.commit()

        async with DatabaseService.session() as db:
            assert await Document.get_latest(db, admin.user_id, "resume.json") is None

    async def test_get_latest_returns_none_when_absent(self, admin):
        async with DatabaseService.session() as db:
            assert await Document.get_latest(db, admin.user_id, "absent.json") is None


PUBLIC_URL = "/public/admin-user/resume/resume.json"
FRONTEND_ORIGIN = "https://resume.example.invalid"


class TestPublicCors:
    """The public routes are read by a browser on another origin.

    Everything asserted here is load-bearing against the Cloudflare cache in
    front of `/public/*` — see middleware/public_cors.py.
    """

    async def test_a_public_read_is_readable_cross_origin(self, client, stored_resume):
        response = await client.get(PUBLIC_URL, headers={"Origin": FRONTEND_ORIGIN})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "*"

    async def test_the_header_is_there_without_a_request_origin(
        self, client, stored_resume
    ):
        """The one that matters behind the edge cache. A request with no
        `Origin` — the deploy smoke test, a monitor — must not be able to fill
        the cache entry with a copy the browser will then reject."""
        response = await client.get(PUBLIC_URL)
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "*"

    async def test_the_response_does_not_vary_on_origin(self, client, stored_resume):
        """Cloudflare honours `Vary` for `Accept-Encoding` only, so a response
        that varies on `Origin` is a response the cache will serve to the wrong
        caller. The wildcard exists so this stays true."""
        response = await client.get(PUBLIC_URL, headers={"Origin": FRONTEND_ORIGIN})
        assert "origin" not in response.headers.get("vary", "").lower()

    async def test_a_missing_document_still_carries_the_header(
        self, client, stored_resume
    ):
        """Otherwise the front end sees an opaque CORS failure where it should
        see a 404 it can handle."""
        response = await client.get(
            "/public/admin-user/resume/absent.json",
            headers={"Origin": FRONTEND_ORIGIN},
        )
        assert response.status_code == 404
        assert response.headers["access-control-allow-origin"] == "*"

    async def test_a_preflight_is_answered(self, client, stored_resume):
        response = await client.options(
            PUBLIC_URL,
            headers={
                "Origin": FRONTEND_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 204
        assert response.headers["access-control-allow-origin"] == "*"
        assert "GET" in response.headers["access-control-allow-methods"]

    async def test_a_preflight_mirrors_the_headers_it_was_asked_about(
        self, client, stored_resume
    ):
        response = await client.options(
            PUBLIC_URL,
            headers={
                "Origin": FRONTEND_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-trace-id",
            },
        )
        assert response.headers["access-control-allow-headers"] == "x-trace-id"

    async def test_the_private_route_is_not_marked_cross_origin_readable(
        self, client, admin, stored_resume
    ):
        response = await client.get(
            "/documents/resume/resume.json",
            headers={**admin.headers, "Origin": FRONTEND_ORIGIN},
        )
        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers

    async def test_the_token_endpoint_is_not_marked_either(
        self, client, admin, password
    ):
        response = await client.post(
            "/token",
            data={"username": admin.username, "password": password},
            headers={"Origin": FRONTEND_ORIGIN},
        )
        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers
