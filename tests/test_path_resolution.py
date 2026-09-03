"""Every path string in `resume.metadata.example.json` and
`resume.skill.example.json` must resolve against the real `ResumePrivate`
model tree — otherwise the document ships instructions that quietly point at
nothing, which nothing else in the stack detects (see Phase 3 of
`V2_MIGRATION_PLAN.md`: the metadata and skill documents still validate under
the new models even when every path inside them is v1-shaped).

Convention, so this file only has to state it once: a path is rooted at the
MCP tool's response, so its first segment is `resume`, `resume_metadata`, or
`resume_skill`; `[]` marks a list element for a human reader but is not
required for resolution here (the model's own field type already says
whether a segment is a list). Every other segment must be a field's
*serialization* name — the camelCase alias for `resume.*`, the plain
(unaliased, so identical) name for `resume_metadata.*` and `resume_skill.*`
— because that is what a client actually sees on the wire.
"""

import json
import types
from pathlib import Path
from typing import Any, ClassVar, Union, get_args, get_origin

import pytest
from pydantic import BaseModel

from services.document.dtos.resume_object import (
    DisclosurePolicy,
    FieldGuide,
    FigurePolicy,
    JoinRule,
    ResumeMetadata,
    ResumePrivate,
    SectionGuide,
    Vocabulary,
)
from services.document.dtos.resume_skill import (
    ContentSelection,
    CoverLetterGuidance,
    OutputSpec,
    ProcedureStep,
    RequiredInput,
    ResumeSkill,
    SkillInput,
    SummaryGuidance,
    TailoringRule,
)


class PathResolver:
    """Walks a dotted, MCP-response-rooted path against the real models."""

    ROOTS: ClassVar[dict[str, type[BaseModel]]] = {
        "resume": ResumePrivate,
        "resume_metadata": ResumeMetadata,
        "resume_skill": ResumeSkill,
    }

    @classmethod
    def looks_like_a_path(cls, value: str) -> bool:
        """`OutputSpec.header_contents[]` is documented as carrying paths *or*
        plain display values ("date", "addressee"); this is how the test
        below tells the two apart rather than guessing."""
        return value in cls.ROOTS or any(
            value.startswith(f"{root}.") for root in cls.ROOTS
        )

    def resolve(self, path: str) -> None:
        root, *rest = path.split(".")
        if root not in self.ROOTS:
            raise ValueError(f"{path!r}: unknown root {root!r}")
        model: Any = self.ROOTS[root]
        for raw in rest:
            name = raw.removesuffix("[]")
            model = self._step(model, name, path)

    def _step(self, model: Any, name: str, path: str) -> Any:
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise TypeError(
                f"{path!r}: {name!r} has nothing to resolve on — "
                f"the path is already at a scalar"
            )
        for field_name, info in model.model_fields.items():
            if name in (info.alias, field_name):
                return self._unwrap(info.annotation)
        raise ValueError(f"{path!r}: {name!r} is not a field of {model.__name__}")

    def _unwrap(self, annotation: Any) -> Any:
        if hasattr(annotation, "__metadata__"):  # Annotated[...]
            return self._unwrap(get_args(annotation)[0])
        origin = get_origin(annotation)
        if origin is types.UnionType or origin is Union:
            candidates = [a for a in get_args(annotation) if a is not type(None)]
            for candidate in candidates:
                unwrapped = self._unwrap(candidate)
                if isinstance(unwrapped, type) and issubclass(unwrapped, BaseModel):
                    return unwrapped
            return self._unwrap(candidates[0]) if candidates else annotation
        if origin is list:
            return self._unwrap(get_args(annotation)[0])
        return annotation


class PathCollector:
    """Every model field documented as carrying a path (or a list of them),
    across `ResumeMetadata` and `ResumeSkill` — see the field list in Phase 3
    of `V2_MIGRATION_PLAN.md`. Walking real, validated document instances
    rather than raw JSON means a path buried in free text (an instruction
    sentence, a `note`) is correctly left alone: only the fields below are
    ever paths.
    """

    PATH_FIELDS: ClassVar[dict[type, frozenset[str]]] = {
        FieldGuide: frozenset({"path"}),
        SectionGuide: frozenset({"path"}),
        Vocabulary: frozenset({"path"}),
        JoinRule: frozenset({"left", "right"}),
        FigurePolicy: frozenset({"figure_path", "amount_path", "basis_path"}),
        DisclosurePolicy: frozenset({"never_publish"}),
        ContentSelection: frozenset({"bullet_source", "judgement_source"}),
        SummaryGuidance: frozenset({"lead_selection_path"}),
        CoverLetterGuidance: frozenset({"story_path", "voice_path"}),
        TailoringRule: frozenset({"applies_to"}),
        SkillInput: frozenset({"read_first"}),
        RequiredInput: frozenset({"resolve_from"}),
        ProcedureStep: frozenset({"reads"}),
        OutputSpec: frozenset({"header_contents"}),
    }

    def collect(self, root: BaseModel) -> list[tuple[str, str]]:
        """Every `(path, description)` pair found, `description` naming
        where it came from for a readable assertion failure."""
        found: list[tuple[str, str]] = []
        self._walk(root, found)
        return found

    def _walk(self, node: Any, found: list[tuple[str, str]]) -> None:
        if isinstance(node, BaseModel):
            path_fields = self.PATH_FIELDS.get(type(node), frozenset())
            for field_name in type(node).model_fields:
                value = getattr(node, field_name)
                if field_name in path_fields:
                    where = f"{type(node).__name__}.{field_name}"
                    found.extend((v, where) for v in self._strings(value))
                else:
                    self._walk(value, found)
        elif isinstance(node, list):
            for item in node:
                self._walk(item, found)

    def _strings(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str)]
        return []


EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


class TestPathsResolve:
    """Loads the two example documents that ship in the repo — the same
    files `DocumentSeeder` writes into every new account — and resolves
    every path they carry."""

    def _paths(self, instance: BaseModel) -> list[tuple[str, str]]:
        collected = PathCollector().collect(instance)
        return [
            (path, where)
            for path, where in collected
            if not where.startswith("OutputSpec.")
            or PathResolver.looks_like_a_path(path)
        ]

    def _assert_all_resolve(self, paths: list[tuple[str, str]]) -> None:
        assert paths, "expected at least one path to check"
        resolver = PathResolver()
        for path, where in paths:
            try:
                resolver.resolve(path)
            except (TypeError, ValueError) as error:
                pytest.fail(
                    f"{where} carries {path!r}, which does not resolve: {error}"
                )

    def test_every_metadata_path_resolves(self):
        raw = json.loads((EXAMPLES_DIR / "resume.metadata.example.json").read_text())[
            "data"
        ]
        instance = ResumeMetadata.model_validate(raw)
        self._assert_all_resolve(self._paths(instance))

    def test_every_skill_path_resolves(self):
        raw = json.loads((EXAMPLES_DIR / "resume.skill.example.json").read_text())[
            "data"
        ]
        instance = ResumeSkill.model_validate(raw)
        self._assert_all_resolve(self._paths(instance))
