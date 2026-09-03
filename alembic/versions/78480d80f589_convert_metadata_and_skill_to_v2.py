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
    """Exact-string mapping from a v1 resume-payload path to its v2
    equivalent, as referenced from a metadata or skill document. See the
    module docstring for provenance."""

    MAPPING: ClassVar[dict[str, str]] = {
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

    @classmethod
    def rewrite_path(cls, value: str) -> str:
        return cls.MAPPING.get(value, value)

    @classmethod
    def rewrite_prose(cls, text: str) -> str:
        for old_path in sorted(cls.MAPPING, key=len, reverse=True):
            if old_path in text:
                text = text.replace(old_path, cls.MAPPING[old_path])
        return text


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
    }
)
MAYBE_PATH_FIELD_NAMES = frozenset({"header_contents"})
PROSE_FIELD_NAMES = frozenset(
    {"readme", "description", "rationale", "rules", "instructions", "note"}
)
PATH_ROOTS = ("resume.", "resume_metadata.", "resume_skill.")
PATH_ROOT_NAMES = frozenset({"resume", "resume_metadata", "resume_skill"})


def _looks_like_a_path(value: str) -> bool:
    return value in PATH_ROOT_NAMES or value.startswith(PATH_ROOTS)


class DocumentRewriter:
    """Recursively rewrites every path-carrying and prose field of a raw
    metadata/skill document, by field name — see the module docstring for
    the field lists and the reasoning behind treating them differently."""

    def rewrite(self, node: Any) -> Any:
        if isinstance(node, dict):
            return {key: self._value(key, value) for key, value in node.items()}
        if isinstance(node, list):
            return [self.rewrite(item) for item in node]
        return node

    def _value(self, key: str, value: Any) -> Any:
        if key in PATH_FIELD_NAMES:
            return self._paths(value)
        if key in MAYBE_PATH_FIELD_NAMES:
            return self._maybe_paths(value)
        if key in PROSE_FIELD_NAMES:
            return self._prose(value)
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
        if isinstance(value, list):
            return [
                PathRewriter.rewrite_path(v)
                if isinstance(v, str) and _looks_like_a_path(v)
                else v
                for v in value
            ]
        return value

    def _prose(self, value: Any) -> Any:
        if isinstance(value, str):
            return PathRewriter.rewrite_prose(value)
        if isinstance(value, list):
            return [
                PathRewriter.rewrite_prose(v) if isinstance(v, str) else v
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
                if key in PROSE_FIELD_NAMES:
                    self._check_prose(old_value, new[key], f"{where}.{key}")
                else:
                    self.check(old_value, new[key], f"{where}.{key}")
        elif isinstance(old, list):
            if not isinstance(new, list) or len(new) < len(old):
                self.dropped.append(where)
                return
            for i, old_item in enumerate(old):
                self.check(old_item, new[i], f"{where}[{i}]")

    def _check_prose(self, old_value: Any, new_value: Any, where: str) -> None:
        if isinstance(old_value, str):
            if new_value != PathRewriter.rewrite_prose(old_value):
                self.prose_mismatches.append(where)
        elif isinstance(old_value, list):
            if not isinstance(new_value, list) or len(new_value) < len(old_value):
                self.dropped.append(where)
                return
            for i, old_item in enumerate(old_value):
                expected = (
                    PathRewriter.rewrite_prose(old_item)
                    if isinstance(old_item, str)
                    else old_item
                )
                if new_value[i] != expected:
                    self.prose_mismatches.append(f"{where}[{i}]")

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
    resolver = SchemaPathResolver()
    return [p for p in PathCollector().collect(new_data) if not resolver.resolves(p)]


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
        if new_data != self.v2_example:
            return "replaced document does not equal the v2 catalogue example"
        return None

    def audit_merge(self, old_data: dict, new_data: dict) -> str | None:
        problems: list[str] = []

        unresolved = _unresolved_paths(new_data)
        if unresolved:
            problems.append(f"  paths that do not resolve: {unresolved}")

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
        print(
            f"user={row.created_by} name={row.name!r} type={row.type}: "
            f"wrote revision {row.revision_id + 1} ({branch} branch)."
        )

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
