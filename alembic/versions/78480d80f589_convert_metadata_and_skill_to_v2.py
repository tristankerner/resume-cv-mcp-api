"""convert metadata and skill documents to v2

Converts every stored metadata/skill document whose latest revision is still
on schema 1 to a production-usable v2 document, by appending a new revision.
Without this, Phase 4 (the previous revision) ships a silent breakage: the
resume becomes v2 while a v1 metadata/skill document still names v1 paths
(`resume.jobs[].highlights[].summary`), which resolve to nothing. Nothing
crashes — `ResumeMetadata`/`ResumeSkill` did not change shape between v1 and
v2, only the path *content* inside documents built against them did — so
validation passes and a tailoring run quietly works from instructions that
reference fields which no longer exist. The public-feed hazard is worse: a
v1 `disclosure.never_publish` names `resume.contact.email_address`, a field
that no longer exists (it is `resume.basics.email` now), so a tailored
document generated from those stale rules could print an address the rules
meant to protect.

**Metadata and skill are product defaults, not user data** — see
`SCHEMA_VERSIONING_PLAN.md` decision 5. Running the shipped defaults
untouched forever must remain a fully supported way to use this, so "the
paths resolve" is not the bar here: this has to produce a document that
drives a tailoring run well on its own, including for v2 capabilities a v1
document never mentioned (per-specific `tech`, the extra vocabularies, and
so on). That rules out a pure path rewrite and is why there are two
branches below rather than one.

**Branch on whether the document was ever customised**, detected by literal
JSON equality against a frozen, validate-then-dump snapshot of the shipped
v1 example (`schema_snapshots/v1_{metadata,skill}.seeded.json` — comparing
against the raw example file would be wrong, since `DocumentSeeder` stores
`model_dump()` output, which materialises nested defaults the example file
omits; verified these are not equal to each other):

- **Untouched -> replace** with the current v2 catalogue example (read from
  `document_schema`, the row the previous-but-one revision inserted). The
  document was the shipped default; it becomes the new shipped default,
  complete and correct. No merge logic, and the audit is one equality check.
- **Customised -> merge.** Keep everything the user wrote, rewrite its
  paths, and additively fill the v2-shaped gaps from the v2 example — the
  user's own wording always wins where the two overlap. The one exception is
  `disclosure.never_publish`, which is *replaced* wholesale: it is a
  disclosure hazard rather than a stylistic choice, and it must agree
  exactly with the code (`PublicProjection.NEVER_PUBLISHED`), which the v2
  catalogue example's list was generated from.

**This migration must not import `services/document/dtos/resume_object.py`
or `resume_skill.py`, for the same reason `b0baccd6558a` does not import
`ResumePrivate`**: a future edit to those modules must not silently change
what an already-applied migration did. Path *resolution* (audit check 1
below) is done against the frozen v2 JSON Schemas in `schema_snapshots/` —
themselves a frozen, versioned snapshot of the model shape, which is exactly
what a JSON Schema is for — rather than against a hand-duplicated parallel
class hierarchy.

`V1_TO_V2_PATHS` is frozen, derived from `ResumePrivate.MIGRATION` as it
stood when this migration was authored, plus one path outside that mapping:
`role_families[].lead_highlight_ids` was already a relative, mis-rooted path
in the v1 catalogue example (missing its `resume_metadata.` root) — not a
resume-model rename, but left unmapped it fails path resolution for a reason
unrelated to this migration, so it is included as a one-off correction.

Anything not in the table is left untouched — and, in the merge branch,
reported by audit check 1 refusing to resolve. That is the intended
outcome: a path this migration does not understand should stop the deploy,
not ship quietly. If a real document turns out to carry one, the fix is to
add it to this frozen table deliberately, not to relax the check.

Revision ID: 78480d80f589
Revises: b0baccd6558a
Create Date: 2026-09-03 08:56:46.930415

"""
import json
import re
from pathlib import Path
from typing import Any, ClassVar, Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "78480d80f589"
down_revision: Union[str, Sequence[str], None] = "b0baccd6558a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REVISION_NOTE = "Migrated to JSON Resume v2 (alembic 78480d80f589)"
SNAPSHOTS = Path(__file__).parent / "schema_snapshots"


def _load(name: str) -> Any:
    return json.loads((SNAPSHOTS / name).read_text())


# --------------------------------------------------------------------------
# Path rewriting
# --------------------------------------------------------------------------


class PathRewriter:
    """Longest-prefix mapping from a v1 resume-payload path to its v2
    equivalent, as referenced from a metadata or skill document. See the
    module docstring for provenance.

    Matching is by *prefix*, not by whole string, and is insensitive to `[]`
    markers on both sides. An exact-string table cannot survive real
    documents: they reference paths at every depth and in both arities —
    `resume.contact.locations[].label`, a bare `resume.jobs`, a trailing
    `resume.jobs[].highlights[].tech[]` — and enumerating every one of those
    is a table nobody can keep complete. Mapping the longest matching prefix
    and carrying the remainder handles the whole family from one entry.

    `[]` is stripped for lookup and re-derived from the frozen v2 schema
    afterwards (`_recase`), because arity can change across the migration:
    v1's `contact.locations[]` is a list, v2's `basics.location` is a single
    object, so `contact.locations[].label` must come out as
    `basics.location.label` and not `basics.location[].label`.
    """

    MAPPING: ClassVar[dict[str, str]] = {
        # The v1 `Profile` model dissolved into `Basics`; without this the
        # whole `resume.profile...` family falls through unmapped.
        "resume.profile": "resume.basics",
        "resume.profile.name": "resume.basics.name",
        "resume.profile.title": "resume.basics.label",
        "resume.profile.tagline": "resume.basics.tagline",
        "resume.summary": "resume.basics.summary",
        "resume.contact.email_address": "resume.basics.email",
        "resume.contact.mobile_number": "resume.basics.phone",
        "resume.contact.locations": "resume.basics.location",
        "resume.contact.locations[]": "resume.basics.location",
        "resume.contact.links[]": "resume.basics.profiles[]",
        "resume.contact.links[].label": "resume.basics.profiles[].network",
        "resume.contact.links[].url": "resume.basics.profiles[].url",
        "resume.contact.links[].publish": "resume.basics.profiles[].publish",
        "resume.contact": "resume.basics",
        "resume.skill_groups[]": "resume.skills[]",
        "resume.skill_groups[].name": "resume.skills[].name",
        "resume.skill_groups[].publish": "resume.skills[].publish",
        "resume.skill_groups[].skills[]": "resume.skills[].keywords[]",
        "resume.skill_groups[].skills[].name": "resume.skills[].keywords[].name",
        "resume.skill_groups[].skills[].url": "resume.skills[].keywords[].url",
        "resume.skill_groups[].skills[].level": "resume.skills[].keywords[].level",
        "resume.skill_groups[].skills[].last_used": "resume.skills[].keywords[].lastUsed",
        "resume.skill_groups[].skills[].publish": "resume.skills[].keywords[].publish",
        "resume.certifications[]": "resume.certificates[]",
        "resume.certifications[].id": "resume.certificates[].identifier",
        "resume.certifications[].name": "resume.certificates[].name",
        "resume.certifications[].url": "resume.certificates[].url",
        "resume.certifications[].publish": "resume.certificates[].publish",
        "resume.jobs[]": "resume.work[]",
        "resume.jobs[].company": "resume.work[].name",
        "resume.jobs[].company_url": "resume.work[].url",
        "resume.jobs[].company_location": "resume.work[].location",
        "resume.jobs[].start": "resume.work[].startDate",
        "resume.jobs[].end": "resume.work[].endDate",
        "resume.jobs[].description": "resume.work[].description",
        "resume.jobs[].publish": "resume.work[].publish",
        "resume.jobs[].role_location": "resume.work[].roleLocation",
        "resume.jobs[].via_employer": "resume.work[].viaEmployer",
        "resume.jobs[].via_employer.name": "resume.work[].viaEmployer.name",
        "resume.jobs[].via_employer.start": "resume.work[].viaEmployer.startDate",
        "resume.jobs[].via_employer.end": "resume.work[].viaEmployer.endDate",
        "resume.jobs[].via_employer.engagement": "resume.work[].viaEmployer.engagement",
        "resume.jobs[].roles[]": "resume.work[].roles[]",
        "resume.jobs[].roles[].title": "resume.work[].roles[].title",
        "resume.jobs[].roles[].start": "resume.work[].roles[].startDate",
        "resume.jobs[].roles[].end": "resume.work[].roles[].endDate",
        "resume.jobs[].highlights[]": "resume.work[].highlights[]",
        "resume.jobs[].highlights[].id": "resume.work[].highlights[].id",
        "resume.jobs[].highlights[].summary": "resume.work[].highlights[].summary",
        "resume.jobs[].highlights[].specifics": "resume.work[].highlights[].specifics[].detail",
        "resume.jobs[].highlights[].specifics[]": "resume.work[].highlights[].specifics[].detail",
        "resume.jobs[].highlights[].tech": "resume.work[].highlights[].tech",
        "resume.jobs[].highlights[].metrics": "resume.work[].highlights[].metrics",
        "resume.jobs[].highlights[].metrics[]": "resume.work[].highlights[].metrics[]",
        "resume.jobs[].highlights[].metrics[].figure": "resume.work[].highlights[].metrics[].figure",
        "resume.jobs[].highlights[].metrics[].amount": "resume.work[].highlights[].metrics[].amount",
        "resume.jobs[].highlights[].metrics[].basis": "resume.work[].highlights[].metrics[].basis",
        "resume.jobs[].highlights[].story": "resume.work[].highlights[].story",
        "resume.jobs[].highlights[].publish": "resume.work[].highlights[].publish",
        "resume.education[].credential": "resume.education[].studyType",
        "resume.education[].field": "resume.education[].area",
        "resume.education[].year": "resume.education[].endDate",
        "resume.personal_projects[]": "resume.projects[]",
        "resume.personal_projects[].name": "resume.projects[].name",
        "resume.personal_projects[].link": "resume.projects[].url",
        "resume.personal_projects[].description": "resume.projects[].description",
        "resume.personal_projects[].publish": "resume.projects[].publish",
        "resume.fine_tuning_data": "resume.fineTuningData",
        "resume.fine_tuning_data.email_address": "resume.basics.email",
        "resume.fine_tuning_data.mobile_number": "resume.basics.phone",
        "resume.fine_tuning_data.narrative": "resume.fineTuningData.narrative",
        "resume.fine_tuning_data.narrative.voice": "resume.fineTuningData.narrative.voice",
        "resume.fine_tuning_data.narrative.career_arc": "resume.fineTuningData.narrative.careerArc",
        "resume.fine_tuning_data.narrative.current_status": "resume.fineTuningData.narrative.currentStatus",
        "resume.fine_tuning_data.narrative.motivation": "resume.fineTuningData.narrative.motivation",
        "resume.fine_tuning_data.narrative.looking_for": "resume.fineTuningData.narrative.lookingFor",
        "resume.fine_tuning_data.narrative.avoiding": "resume.fineTuningData.narrative.avoiding",
        "resume.fine_tuning_data.narrative.working_style": "resume.fineTuningData.narrative.workingStyle",
        "resume.fine_tuning_data.narrative.strengths": "resume.fineTuningData.narrative.strengths",
        "resume.fine_tuning_data.logistics": "resume.fineTuningData.logistics",
        "role_families[].lead_highlight_ids": "resume_metadata.role_families[].lead_highlight_ids",
    }

    # v1 identifiers that appear bare in prose rather than as a rooted path
    # ("fine_tuning_data.logistics is background only", a `trim_order` entry
    # reading "personal_projects"). Prose-only: they are not paths and must
    # never be fed to `rewrite_path`.
    BARE_PROSE: ClassVar[dict[str, str]] = {
        "fine_tuning_data": "fineTuningData",
        "skill_groups": "skills",
        "personal_projects": "projects",
    }

    @staticmethod
    def _normalise(path: str) -> str:
        """Drop `[]` from every segment, so lookup is arity-insensitive."""
        return ".".join(seg.removesuffix("[]") for seg in path.split("."))

    @classmethod
    def _recase(cls, normalised_v2: str) -> str:
        """Re-insert `[]` on every non-terminal array segment, per the frozen
        v2 schema. Terminal scalar lists carry no marker, matching the v2
        catalogue example's own convention (`...highlights[].tech`)."""
        segments = normalised_v2.split(".")
        out = [segments[0]]
        for index in range(1, len(segments)):
            prefix = ".".join(segments[: index + 1])
            terminal = index == len(segments) - 1
            marker = "[]" if not terminal and _is_array(prefix) else ""
            out.append(segments[index] + marker)
        return ".".join(out)

    @classmethod
    def _rewrite_single(cls, value: str) -> str:
        value = value.strip()
        if not _looks_like_a_path(value):
            return value
        segments = cls._normalise(value).split(".")
        for cut in range(len(segments), 0, -1):
            prefix = ".".join(segments[:cut])
            if prefix in _NORMALISED_MAPPING:
                mapped = [_NORMALISED_MAPPING[prefix], *segments[cut:]]
                return cls._recase(".".join(mapped))
        return cls._recase(".".join(segments))

    @classmethod
    def rewrite_path(cls, value: str) -> str:
        """One path field's value. Real documents put more than one path in a
        single field (`"resume.education, resume.certifications"`), so a
        comma-joined list of paths is split, mapped and rejoined; anything
        else is treated as one value. A value that is not path-shaped at all
        — a display keyword like `header`, a URL — is returned untouched."""
        parts = [part.strip() for part in value.split(",")]
        if len(parts) > 1 and all(_looks_like_a_path(part) for part in parts if part):
            return ", ".join(cls._rewrite_single(part) for part in parts)
        return cls._rewrite_single(value)

    @classmethod
    def rewrite_prose(cls, text: str) -> str:
        for old, new in _PROSE_SUBSTITUTIONS:
            if old in text:
                text = text.replace(old, new)
        return text


# Built once from MAPPING. `_NORMALISED_MAPPING` is the arity-insensitive
# index prefix matching walks; `_PROSE_SUBSTITUTIONS` is every spelling a v1
# reference can take in prose — rooted, `[]`-less, and bare — longest-first so
# a shorter key cannot shadow a longer one it is a prefix of.
_NORMALISED_MAPPING: dict[str, str] = {
    PathRewriter._normalise(old): PathRewriter._normalise(new)
    for old, new in PathRewriter.MAPPING.items()
}
_PROSE_SUBSTITUTIONS: list[tuple[str, str]] = sorted(
    {
        **PathRewriter.MAPPING,
        **_NORMALISED_MAPPING,
        **PathRewriter.BARE_PROSE,
    }.items(),
    key=lambda item: len(item[0]),
    reverse=True,
)

PATH_FIELD_NAMES = frozenset(
    {
        "path",
        "left",
        "right",
        "figure_path",
        "amount_path",
        "basis_path",
        "bullet_source",
        "judgement_source",
        "lead_selection_path",
        "story_path",
        "voice_path",
        "applies_to",
        "read_first",
        "resolve_from",
        "reads",
        # Both carry paths and were missing here. `never_publish` is replaced
        # wholesale in the merge branch so it survived by luck; `sources` is
        # unioned, so its v1 entries were being kept verbatim and silently
        # left pointing at fields that no longer exist.
        "never_publish",
        "sources",
    }
)
MAYBE_PATH_FIELD_NAMES = frozenset({"header_contents"})
PATH_ROOTS = ("resume.", "resume_metadata.", "resume_skill.")
PATH_ROOT_NAMES = frozenset({"resume", "resume_metadata", "resume_skill"})


def _looks_like_a_path(value: str) -> bool:
    return value in PATH_ROOT_NAMES or value.startswith(PATH_ROOTS)


def _is_array(v2_path: str) -> bool:
    """Is this v2 path an array, per the frozen schema? Drives `_recase`."""
    root_name, *rest = v2_path.split(".")
    if root_name not in SchemaPathResolver.ROOTS:
        return False
    root = SchemaPathResolver.ROOTS[root_name]
    node: Any = root
    for raw in rest:
        node = _deref(node, root)
        if node.get("type") == "array" and "items" in node:
            node = _deref(node["items"], root)
        if not isinstance(node, dict) or "properties" not in node:
            return False
        name = raw.removesuffix("[]")
        if name not in node["properties"]:
            return False
        node = node["properties"][name]
    return _deref(node, root).get("type") == "array"


def _deref(node: Any, root: dict) -> Any:
    """Follow `$ref` and `anyOf`, but stop at an array rather than descending
    into its items — the caller needs to see that it *is* an array."""
    while isinstance(node, dict):
        if "$ref" in node:
            node = root["$defs"][node["$ref"].split("/")[-1]]
            continue
        if "anyOf" in node:
            candidates = [c for c in node["anyOf"] if c.get("type") != "null"]
            node = candidates[0] if candidates else node["anyOf"][0]
            continue
        break
    return node


class DocumentRewriter:
    """Recursively rewrites a raw metadata/skill document: path fields by
    the mapping, everything else as prose.

    Prose handling is a denylist, not an allowlist. An earlier version listed
    the prose fields by name and substituted only those, which meant every
    free-text field nobody thought of — `rule`, `condition`, `never_claim`,
    `structure`, `cautions` — kept its v1 references and passed every audit,
    because the audits only check paths. Treating *any* string that is not a
    path field as prose removes that whole class of miss: the substitution is
    driven by the frozen mapping table and only ever replaces exact v1
    spellings, so applying it more widely cannot invent a change.
    """

    def rewrite(self, node: Any) -> Any:
        if isinstance(node, dict):
            return {key: self._value(key, value) for key, value in node.items()}
        if isinstance(node, list):
            return [self.rewrite(item) for item in node]
        if isinstance(node, str):
            return PathRewriter.rewrite_prose(node)
        return node

    def _value(self, key: str, value: Any) -> Any:
        if key in PATH_FIELD_NAMES:
            return self._paths(value)
        if key in MAYBE_PATH_FIELD_NAMES:
            return self._maybe_paths(value)
        return self.rewrite(value)

    def _paths(self, value: Any) -> Any:
        if isinstance(value, str):
            return PathRewriter.rewrite_path(value)
        if isinstance(value, list):
            return [
                PathRewriter.rewrite_path(v) if isinstance(v, str) else v
                for v in value
            ]
        return value

    def _maybe_paths(self, value: Any) -> Any:
        """`header_contents` mixes paths with display words ("date",
        "addressee"). Path-shaped entries are mapped; the rest are prose."""
        if isinstance(value, list):
            return [
                PathRewriter.rewrite_path(v)
                if isinstance(v, str) and _looks_like_a_path(v)
                else self.rewrite(v)
                for v in value
            ]
        return value


# --------------------------------------------------------------------------
# Path resolution, against the frozen v2 JSON Schemas — see module docstring
# for why this reads schema_snapshots/ rather than importing live models.
# --------------------------------------------------------------------------


class SchemaPathResolver:
    ROOTS: ClassVar[dict[str, Any]] = {
        "resume": _load("v2_resume.schema.json"),
        "resume_metadata": _load("v2_metadata.schema.json"),
        "resume_skill": _load("v2_skill.schema.json"),
    }

    def _unwrap(self, node: Any, root_schema: dict) -> Any:
        while True:
            if "$ref" in node:
                node = root_schema["$defs"][node["$ref"].split("/")[-1]]
                continue
            if "anyOf" in node:
                candidates = [c for c in node["anyOf"] if c.get("type") != "null"]
                node = candidates[0] if candidates else node["anyOf"][0]
                continue
            if node.get("type") == "array" and "items" in node:
                node = node["items"]
                continue
            break
        return node

    def resolves(self, path: str) -> bool:
        root, *rest = path.split(".")
        if root not in self.ROOTS:
            return False
        root_schema = self.ROOTS[root]
        node = root_schema
        for raw in rest:
            name = raw.removesuffix("[]")
            node = self._unwrap(node, root_schema)
            if not isinstance(node, dict) or "properties" not in node:
                return False
            props = node["properties"]
            if name not in props:
                return False
            node = props[name]
        return True


class PathCollector:
    """Every path-carrying value in a document, by the same field-name
    classification `DocumentRewriter` rewrites by."""

    def collect(self, node: Any) -> list[str]:
        found: list[str] = []
        self._walk(node, found)
        return found

    def _walk(self, node: Any, found: list[str]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in PATH_FIELD_NAMES:
                    found.extend(self._strings(value))
                elif key in MAYBE_PATH_FIELD_NAMES:
                    found.extend(v for v in self._strings(value) if _looks_like_a_path(v))
                else:
                    self._walk(value, found)
        elif isinstance(node, list):
            for item in node:
                self._walk(item, found)

    def _strings(self, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [v for v in value if isinstance(v, str)]
        return []


# --------------------------------------------------------------------------
# Audits
# --------------------------------------------------------------------------


class ConversionFailure(Exception):
    """One document failed conversion or audit. See `b0baccd6558a` — the
    same reasoning applies: every candidate is checked before anything is
    written, and one failure aborts the whole migration."""


class StructureAndProseAudit:
    """Merge-branch checks 2 and 3: nothing the user wrote was dropped, and
    every prose field changed only by the known literal path substitution.

    Combined into one recursive walk because both need the same thing: the
    old and new document walked in parallel, matching dicts by key and lists
    by index. That parallel walk is valid here specifically because the
    merge branch only ever rewrites a field's *value* in place or *appends*
    to a list — it never removes a key or reorders/inserts before the end of
    a list — so the original structure is always a prefix of the new one.
    """

    def __init__(self) -> None:
        self.dropped: list[str] = []
        self.prose_mismatches: list[str] = []

    def check(self, old: Any, new: Any, where: str = "") -> None:
        if isinstance(old, dict):
            if not isinstance(new, dict):
                self.dropped.append(where or "<root>")
                return
            for key, old_value in old.items():
                if key not in new:
                    self.dropped.append(f"{where}.{key}")
                    continue
                if key in PATH_FIELD_NAMES or key in MAYBE_PATH_FIELD_NAMES:
                    # Paths are covered by resolution (check 1), not here.
                    continue
                self.check(old_value, new[key], f"{where}.{key}")
        elif isinstance(old, list):
            if not isinstance(new, list) or len(new) < len(old):
                self.dropped.append(where)
                return
            for i, old_item in enumerate(old):
                self.check(old_item, new[i], f"{where}[{i}]")
        elif isinstance(old, str):
            # Every string outside a path field is prose, and may differ only
            # by the known literal substitution.
            if new != PathRewriter.rewrite_prose(old):
                self.prose_mismatches.append(where or "<root>")

    @property
    def clean(self) -> bool:
        return not self.dropped and not self.prose_mismatches

    def describe(self) -> str:
        lines = []
        if self.dropped:
            lines.append(f"  dropped: {self.dropped}")
        if self.prose_mismatches:
            lines.append(f"  prose changed beyond the known substitution: {self.prose_mismatches}")
        return "\n".join(lines)


def _unresolved_paths(new_data: dict) -> list[str]:
    """Path-shaped values that do not resolve against the frozen v2 schema.

    Non-path-shaped values (a display keyword, a URL) are excluded rather
    than failed — see `_maybe_paths`. They are counted by `_skipped_values`
    so that exclusion is reported rather than silent.
    """
    resolver = SchemaPathResolver()
    unresolved = []
    for collected in PathCollector().collect(new_data):
        for part in _split_paths(collected):
            if _looks_like_a_path(part) and not resolver.resolves(part):
                unresolved.append(part)
    return unresolved


def _skipped_values(new_data: dict) -> list[str]:
    """Values in a path field that are not path-shaped, so were left alone."""
    return [
        part
        for collected in PathCollector().collect(new_data)
        for part in _split_paths(collected)
        if not _looks_like_a_path(part)
    ]


def _split_paths(value: str) -> list[str]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) > 1 and all(_looks_like_a_path(part) for part in parts if part):
        return parts
    return [value]


# Any surviving reference to a v1-only construct, rooted or bare. This is the
# check path resolution cannot make: a stale reference sitting in prose is
# invisible to `_unresolved_paths`, resolves against nothing, and would ship
# as an instruction naming a field that no longer exists.
V1_RESIDUE = re.compile(
    r"\bresume\.(?:profile|contact|jobs|skill_groups|certifications"
    r"|personal_projects|fine_tuning_data)\b[A-Za-z_\[\]\.]*"
    r"|\bfine_tuning_data\b|\bskill_groups\b|\bpersonal_projects\b"
)


def _residual_v1_references(node: Any, where: str = "") -> list[str]:
    """Every place a v1 reference survived, anywhere in the document."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.extend(_residual_v1_references(value, f"{where}.{key}"))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(_residual_v1_references(item, f"{where}[{index}]"))
    elif isinstance(node, str):
        for hit in V1_RESIDUE.findall(node):
            found.append(f"{where or '<root>'}: {hit}")
    return found


# --------------------------------------------------------------------------
# Additive gap-filling (merge branch only)
# --------------------------------------------------------------------------


def _merge_by_path(existing: list[dict], from_v2: list[dict], subfield: str | None) -> list[dict]:
    """Appends any `from_v2` entry whose `path` is absent from `existing`.
    For an entry that *is* already present, additively appends any of its
    `subfield` items (matched the same way) that are missing. Mutates and
    returns `existing`."""
    by_path = {item["path"]: item for item in existing if "path" in item}
    for v2_item in from_v2:
        current = by_path.get(v2_item["path"])
        if current is None:
            existing.append(v2_item)
            by_path[v2_item["path"]] = v2_item
        elif subfield and subfield in v2_item:
            current_children = current.setdefault(subfield, [])
            known = {c["path"] for c in current_children if "path" in c}
            for child in v2_item[subfield]:
                if child["path"] not in known:
                    current_children.append(child)
                    known.add(child["path"])
    return existing


def _union_preserving_order(existing: list[str], from_v2: list[str]) -> list[str]:
    merged = list(existing)
    for value in from_v2:
        if value not in merged:
            merged.append(value)
    return merged


# --------------------------------------------------------------------------
# Per-type conversion
# --------------------------------------------------------------------------


class DocumentTypeConverter:
    def __init__(self, document_type: str, v1_seeded: dict, v2_example: dict) -> None:
        self.document_type = document_type
        self.v1_seeded = v1_seeded
        self.v2_example = v2_example

    def is_untouched(self, data: dict) -> bool:
        return data == self.v1_seeded

    def replace(self) -> dict:
        return self.v2_example

    def merge(self, data: dict) -> dict:
        new_data = DocumentRewriter().rewrite(data)
        if self.document_type == "metadata":
            new_data["disclosure"]["never_publish"] = list(
                self.v2_example["disclosure"]["never_publish"]
            )
            new_data["sections"] = _merge_by_path(
                new_data.get("sections", []), self.v2_example.get("sections", []), "fields"
            )
            new_data["vocabularies"] = _merge_by_path(
                new_data.get("vocabularies", []),
                self.v2_example.get("vocabularies", []),
                None,
            )
        elif self.document_type == "skill":
            if "content_selection" in new_data and new_data["content_selection"]:
                new_data["content_selection"]["judgement_source"] = (
                    "resume.work[].highlights[].specifics[].detail"
                )
            if "keywords" in new_data and new_data["keywords"]:
                new_data["keywords"]["sources"] = _union_preserving_order(
                    new_data["keywords"].get("sources", []),
                    self.v2_example.get("keywords", {}).get("sources", []),
                )
        return new_data

    def audit_replace(self, new_data: dict) -> str | None:
        problems: list[str] = []
        if new_data != self.v2_example:
            problems.append("  replaced document does not equal the v2 catalogue example")
        residual = _residual_v1_references(new_data)
        if residual:
            problems.append(f"  v1 references survive in the v2 example: {residual}")
        return "\n".join(problems) if problems else None

    def audit_merge(self, old_data: dict, new_data: dict) -> str | None:
        problems: list[str] = []

        unresolved = _unresolved_paths(new_data)
        if unresolved:
            problems.append(f"  paths that do not resolve: {unresolved}")

        # The check resolution cannot make. A v1 reference left in prose
        # resolves against nothing and so is invisible above, but it is an
        # instruction naming a field that no longer exists — exactly the
        # silent breakage this migration exists to prevent.
        residual = _residual_v1_references(new_data)
        if residual:
            problems.append(f"  v1 references that survived conversion: {residual}")

        structure = StructureAndProseAudit()
        structure.check(old_data, new_data)
        if not structure.clean:
            problems.append(structure.describe())

        if self.document_type == "metadata":
            if new_data["disclosure"]["never_publish"] != list(
                self.v2_example["disclosure"]["never_publish"]
            ):
                problems.append("  never_publish does not equal the v2 catalogue example's")

        return "\n".join(problems) if problems else None


def _documents_table() -> sa.Table:
    return sa.Table(
        "documents",
        sa.MetaData(),
        sa.Column("created_by", sa.Integer()),
        sa.Column("name", sa.String()),
        sa.Column("revision_id", sa.Integer()),
        sa.Column("type", sa.String()),
        sa.Column("public", sa.Boolean()),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("revision_note", sa.String()),
        sa.Column("data", sa.JSON()),
        sa.Column("schema_version", sa.Integer()),
    )


def _document_schema_table() -> sa.Table:
    return sa.Table(
        "document_schema",
        sa.MetaData(),
        sa.Column("version", sa.Integer()),
        sa.Column("document_type", sa.String()),
        sa.Column("example", sa.JSON()),
    )


def _v2_example(conn: sa.Connection, document_type: str) -> dict:
    table = _document_schema_table()
    row = conn.execute(
        sa.select(table.c.example).where(
            table.c.document_type == document_type, table.c.version == 2
        )
    ).scalar_one()
    return row


def _latest_by_key(rows: list[Any]) -> list[Any]:
    latest: dict[tuple[int, str], Any] = {}
    for row in rows:
        key = (row.created_by, row.name)
        current = latest.get(key)
        if current is None or row.revision_id > current.revision_id:
            latest[key] = row
    return list(latest.values())


def upgrade() -> None:
    """Upgrade schema."""
    from datetime import UTC, datetime

    conn = op.get_bind()
    table = _documents_table()

    converters = {
        "metadata": DocumentTypeConverter(
            "metadata", _load("v1_metadata.seeded.json"), _v2_example(conn, "metadata")
        ),
        "skill": DocumentTypeConverter(
            "skill", _load("v1_skill.seeded.json"), _v2_example(conn, "skill")
        ),
    }

    rows = conn.execute(
        sa.select(table).where(table.c.type.in_(("metadata", "skill")))
    ).fetchall()
    candidates = [row for row in _latest_by_key(rows) if row.schema_version == 1]

    conversions: list[tuple[Any, dict[str, Any], str]] = []
    failures: list[str] = []
    for row in candidates:
        label = f"user={row.created_by} name={row.name!r} type={row.type}"
        converter = converters[row.type]
        try:
            if converter.is_untouched(row.data):
                branch = "replace"
                new_data = converter.replace()
                problem = converter.audit_replace(new_data)
            else:
                branch = "merge"
                new_data = converter.merge(row.data)
                problem = converter.audit_merge(row.data, new_data)
        except Exception as error:  # noqa: BLE001 - reported, then re-raised
            failures.append(f"{label}: FAILED to convert — {type(error).__name__}: {error}")
            continue

        if problem:
            failures.append(f"{label}: audit failed ({branch} branch):\n{problem}")
            continue

        conversions.append((row, new_data, branch))

    if failures:
        raise ConversionFailure(
            "Refusing to convert any metadata/skill document: "
            f"{len(failures)} of {len(candidates)} failed audit or conversion.\n"
            + "\n".join(failures)
        )

    now = datetime.now(UTC).replace(tzinfo=None)
    for row, new_data, branch in conversions:
        conn.execute(
            table.insert().values(
                created_by=row.created_by,
                name=row.name,
                revision_id=row.revision_id + 1,
                type=row.type,
                public=row.public,
                created_at=now,
                revision_note=REVISION_NOTE,
                data=new_data,
                schema_version=2,
            )
        )
        # Reported rather than silent: these are values sitting in a path
        # field that are not path-shaped — a display keyword, a URL — so the
        # conversion left them alone and the resolution audit skipped them.
        # If one of them ought to have been a path, this line is the only
        # place that would show it.
        skipped = _skipped_values(new_data)
        print(
            f"user={row.created_by} name={row.name!r} type={row.type}: "
            f"wrote revision {row.revision_id + 1} ({branch} branch), "
            f"{len(skipped)} non-path value(s) left untouched."
        )
        for value in skipped:
            print(f"    left as-is: {value}")

    print(f"{len(candidates)} metadata/skill document(s) found; {len(conversions)} converted.")


def downgrade() -> None:
    """Downgrade schema. Same reasoning as `b0baccd6558a`'s downgrade: delete
    only what this migration appended, identified by both `schema_version`
    and `revision_note`, and refuse if a newer revision now sits above one."""
    conn = op.get_bind()
    table = _documents_table()

    marked = conn.execute(
        sa.select(table).where(
            table.c.schema_version == 2,
            table.c.revision_note == REVISION_NOTE,
            table.c.type.in_(("metadata", "skill")),
        )
    ).fetchall()

    blocked = []
    for row in marked:
        newer = conn.execute(
            sa.select(sa.func.count())
            .select_from(table)
            .where(
                table.c.created_by == row.created_by,
                table.c.name == row.name,
                table.c.revision_id > row.revision_id,
            )
        ).scalar()
        if newer:
            blocked.append(f"user={row.created_by} name={row.name!r} revision={row.revision_id}")

    if blocked:
        raise RuntimeError(
            "Cannot downgrade: a newer revision exists above a revision this "
            "migration appended, for: " + "; ".join(blocked) + ". Resolve by "
            "hand — deleting the marked revision would leave the history "
            "claiming a lineage that never existed."
        )

    for row in marked:
        conn.execute(
            table.delete().where(
                table.c.created_by == row.created_by,
                table.c.name == row.name,
                table.c.revision_id == row.revision_id,
            )
        )
