"""Structural guarantees for `PublicProjection`: every private field is
declared exactly once as a difference between a private model and its public
counterpart, and none of them can reach a served document regardless of what
model changes forget to update `PublicProjection.NEVER_PUBLISHED`.

See `services.document.dtos.resume_object.PublicProjection` for why this is
two tests rather than one: the structural-drift test below catches a private
field that was added to a model and never carried into its `Public*`
counterpart — the mistake that actually happens when a model grows a field.
The sentinel-leak test catches the same mistake from the other side: even if
every model here is wired correctly today, it proves `PublicProjection.build`
raises rather than serves if that ever stops being true, because the value
would have nowhere to validate into on `ResumePublic`.
"""

import json
from typing import ClassVar

import pytest

from services.document.dtos.resume_object import (
    Award,
    Basics,
    Certificate,
    Education,
    Highlight,
    Interest,
    Keyword,
    Language,
    Location,
    Profile,
    Project,
    Publication,
    PublicAward,
    PublicBasics,
    PublicCertificate,
    PublicEducation,
    PublicHighlight,
    PublicInterest,
    PublicKeyword,
    PublicLanguage,
    PublicLocation,
    PublicProfile,
    PublicProject,
    PublicProjection,
    PublicPublication,
    PublicReference,
    PublicSkill,
    PublicSpecific,
    PublicVolunteer,
    PublicWork,
    Reference,
    ResumePrivate,
    ResumePublic,
    Skill,
    Specific,
    Volunteer,
    Work,
)

# Each pair is (private model, its public counterpart, the field names that
# must be present on the private model and absent from the public one). Every
# model in the resume payload's tree belongs here once — `Role`, `ViaEmployer`
# and `Meta` are absent because they carry no private field and are reused
# as-is in `ResumePublic` rather than mirrored.
PAIRS: list[tuple[type, type, set[str]]] = [
    (ResumePrivate, ResumePublic, {"fine_tuning_data"}),
    (Basics, PublicBasics, {"email", "phone"}),
    (Location, PublicLocation, {"publish"}),
    (Profile, PublicProfile, {"publish"}),
    (Work, PublicWork, {"publish"}),
    (Volunteer, PublicVolunteer, {"publish"}),
    (Highlight, PublicHighlight, {"publish", "tech", "metrics", "story"}),
    (Specific, PublicSpecific, {"tech"}),
    (Education, PublicEducation, {"publish"}),
    (Award, PublicAward, {"publish"}),
    (Certificate, PublicCertificate, {"publish"}),
    (Publication, PublicPublication, {"publish"}),
    (Skill, PublicSkill, {"publish"}),
    (Keyword, PublicKeyword, {"publish", "level", "last_used"}),
    (Language, PublicLanguage, {"publish"}),
    (Interest, PublicInterest, {"publish"}),
    (Reference, PublicReference, {"publish"}),
    (Project, PublicProject, {"publish"}),
]


class TestStructuralDrift:
    """Walks `ResumePrivate` and `ResumePublic` in parallel, model by model."""

    @pytest.mark.parametrize(
        "private, public, declared_private",
        PAIRS,
        ids=[pair[0].__name__ for pair in PAIRS],
    )
    def test_field_sets_match_exactly(self, private, public, declared_private):
        private_fields = set(private.model_fields)
        public_fields = set(public.model_fields)
        assert private_fields - public_fields == declared_private
        assert public_fields - private_fields == set()


class TestSentinelLeak:
    """Every string-typed private field gets its own unique sentinel; a
    handful of private fields are not strings (`publish` is a bool, `level`
    is a closed vocabulary) and cannot carry one, so those are asserted by key
    instead, in `test_non_string_private_fields_are_absent`.
    """

    SENTINELS: ClassVar[dict[str, str]] = {
        "email": "SENTINEL-basics-email",
        "phone": "SENTINEL-basics-phone",
        "tech": "SENTINEL-highlight-tech",
        "specific_tech": "SENTINEL-specific-tech",
        "metric_figure": "SENTINEL-metric-figure",
        "metric_amount": "SENTINEL-metric-amount",
        "metric_basis": "SENTINEL-metric-basis",
        "story": "SENTINEL-highlight-story",
        "narrative": "SENTINEL-fine-tuning-narrative",
        "logistics": "SENTINEL-fine-tuning-logistics",
    }

    def _payload(self) -> dict:
        s = self.SENTINELS
        return {
            "basics": {
                "name": "Sentinel Test",
                "email": s["email"],
                "phone": s["phone"],
                "location": {"label": "Remote", "kind": "remote", "note": "note"},
            },
            "work": [
                {
                    "name": "Company",
                    "highlights": [
                        {
                            "id": "highlight-1",
                            "summary": "summary",
                            "specifics": [
                                {"detail": "detail", "tech": [s["specific_tech"]]}
                            ],
                            "tech": [s["tech"]],
                            "metrics": [
                                {
                                    "figure": s["metric_figure"],
                                    "amount": s["metric_amount"],
                                    "basis": s["metric_basis"],
                                }
                            ],
                            "story": s["story"],
                        }
                    ],
                }
            ],
            "skills": [
                {
                    "name": "group",
                    "keywords": [
                        {"name": "Python", "level": "expert", "lastUsed": "1875"}
                    ],
                }
            ],
            "fineTuningData": {
                "narrative": {"career_arc": s["narrative"]},
                "logistics": {"work_authorization": s["logistics"]},
            },
        }

    def test_no_sentinel_reaches_the_public_projection(self):
        private = ResumePrivate.model_validate(self._payload())
        public = PublicProjection.of(private)
        body = json.dumps(public.model_dump())
        for sentinel in self.SENTINELS.values():
            assert sentinel not in body
        assert "1875" not in body  # keyword.lastUsed, Iso8601-constrained

    def test_non_string_private_fields_are_absent(self):
        payload = self._payload()
        payload["work"][0]["publish"] = True
        payload["skills"][0]["publish"] = True
        payload["skills"][0]["keywords"][0]["publish"] = True
        private = ResumePrivate.model_validate(payload)
        public = PublicProjection.of(private)
        body = json.dumps(public.model_dump())
        assert "publish" not in body
        assert "expert" not in body  # keyword.level
