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
import types
from pathlib import Path
from typing import Any, ClassVar, Union, get_args, get_origin

import pytest
from pydantic import BaseModel

from services.document.dtos.resume_object import (
    Award,
    Basics,
    Certificate,
    Education,
    FineTuningData,
    Highlight,
    Interest,
    Keyword,
    Language,
    Location,
    Logistics,
    Meta,
    Metric,
    Narrative,
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
    ResumeMetadata,
    ResumePrivate,
    ResumePublic,
    Role,
    Skill,
    Specific,
    ViaEmployer,
    Volunteer,
    Work,
)

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


class Annotations:
    """The model behind a field annotation, past `Optional`, `list` and
    `Annotated`."""

    @classmethod
    def inner(cls, annotation: Any) -> Any:
        if hasattr(annotation, "__metadata__"):
            return cls.inner(get_args(annotation)[0])
        origin = get_origin(annotation)
        if origin is types.UnionType or origin is Union:
            for arg in get_args(annotation):
                if arg is not type(None):
                    return cls.inner(arg)
        if origin is list:
            return cls.inner(get_args(annotation)[0])
        return annotation

    @classmethod
    def model(cls, annotation: Any) -> type[BaseModel] | None:
        inner = cls.inner(annotation)
        if isinstance(inner, type) and issubclass(inner, BaseModel):
            return inner
        return None


class ModelTree:
    """Every model reachable from a root, computed rather than restated."""

    @classmethod
    def reachable_from(cls, root: type[BaseModel]) -> set[type[BaseModel]]:
        found: set[type[BaseModel]] = set()
        cls()._walk(root, found)
        return found

    def _walk(self, model: type[BaseModel], found: set[type[BaseModel]]) -> None:
        if model in found:
            return
        found.add(model)
        for info in model.model_fields.values():
            nested = Annotations.model(info.annotation)
            if nested is not None:
                self._walk(nested, found)


class RedactionPaths:
    """`PublicProjection.NEVER_PUBLISHED` flattened into the dotted,
    client-facing notation the metadata document writes its paths in, so the
    two can be compared directly instead of by eye."""

    @classmethod
    def of(cls, spec: dict, model: type[BaseModel], prefix: str = "resume") -> set[str]:
        return cls()._walk(spec, model, prefix)

    def _walk(self, spec: dict, model: type[BaseModel], prefix: str) -> set[str]:
        found: set[str] = set()
        for key, value in spec.items():
            if key == "__all__":
                found |= self._elements(value, model, f"{prefix}[]")
                continue
            info = model.model_fields[key]
            path = f"{prefix}.{info.alias or key}"
            if value is True:
                found.add(path)
                continue
            nested = Annotations.model(info.annotation)
            assert nested is not None, f"{path} redacts into a non-model"
            found |= self._elements(value, nested, path)
        return found

    def _elements(self, value: Any, model: type[BaseModel], prefix: str) -> set[str]:
        if isinstance(value, set):
            return {
                f"{prefix}.{model.model_fields[name].alias or name}" for name in value
            }
        return self._walk(value, model, prefix)


# Reused verbatim inside `ResumePublic` rather than mirrored, because they
# carry no private field. `test_shared_models_carry_nothing_private` is what
# holds that claim true.
SHARED: set[type[BaseModel]] = {Role, ViaEmployer, Meta}

# Never reach `ResumePublic` at all: `NEVER_PUBLISHED` drops the field that
# holds them, so there is nothing to mirror and nothing to compare.
PRIVATE_ONLY: set[type[BaseModel]] = {Metric, FineTuningData, Narrative, Logistics}

# Each pair is (private model, its public counterpart, the field names that
# must be present on the private model and absent from the public one). Every
# model in the private tree belongs here, in `SHARED`, or in `PRIVATE_ONLY` —
# `test_pairs_covers_every_model_in_the_private_tree` is what enforces that.
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

# Every field name any pair declares private, for the shared-model check.
PRIVATE_FIELD_NAMES: set[str] = {name for _, _, declared in PAIRS for name in declared}


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

    def test_pairs_covers_every_model_in_the_private_tree(self):
        """`PAIRS` is written by hand, so it needs its own guard.

        Without this, adding a section to `ResumePrivate` and forgetting to
        list it above leaves the new model with no drift coverage at all —
        the test suite stays green while the thing it exists to check quietly
        stops being checked. Reachability is computed from the model tree
        rather than restated, so the only way to satisfy it is to add the
        model to `PAIRS` or to `SHARED`.
        """
        covered = {pair[0] for pair in PAIRS} | SHARED | PRIVATE_ONLY
        missing = ModelTree.reachable_from(ResumePrivate) - covered
        assert missing == set(), (
            "these models are in the private tree but neither paired with a "
            f"Public* counterpart nor declared shared: "
            f"{sorted(model.__name__ for model in missing)}"
        )

    def test_shared_models_carry_nothing_private(self):
        """A model reused verbatim in `ResumePublic` must have no private
        field to reuse — `SHARED` is a claim, and this is what checks it."""
        for model in SHARED:
            leaked = set(model.model_fields) & PRIVATE_FIELD_NAMES
            assert leaked == set(), f"{model.__name__} carries {sorted(leaked)}"


class TestDisclosureMatchesCode:
    """`resume.metadata.example.json` tells the MCP client what must never be
    published; `PublicProjection.NEVER_PUBLISHED` is what actually withholds
    it. They agree today by hand. This is what keeps them agreeing — a client
    told a stale disclosure list is a client reasoning about the wrong
    document, and nothing else in the stack compares the two.
    """

    def test_declared_disclosure_matches_what_the_code_redacts(self):
        raw = json.loads((EXAMPLES_DIR / "resume.metadata.example.json").read_text())[
            "data"
        ]
        declared = set(ResumeMetadata.model_validate(raw).disclosure.never_publish)
        enforced = RedactionPaths.of(PublicProjection.NEVER_PUBLISHED, ResumePrivate)

        assert enforced - declared == set(), (
            "redacted in code but not declared in the metadata document: "
            f"{sorted(enforced - declared)}"
        )
        assert declared - enforced == set(), (
            "declared in the metadata document but not redacted in code: "
            f"{sorted(declared - enforced)}"
        )


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
