"""convert resume documents to v2

Converts every stored resume document whose latest revision is still
v1-shaped (no `basics` key) to the JSON Resume v2 shape, by appending a new
revision — never rewriting one. `documents` carries a no-UPDATE trigger and
had its no-DELETE trigger removed in `35808d4a4c1d`, so an append-only
conversion needs no trigger manipulation at all.

This is the migration `main.py`'s in-process `alembic upgrade head` runs
against production on the next deploy, unattended — see
`SCHEMA_VERSIONING_PLAN.md`, "The consequence you have accepted". Every
document is converted and audited *before* anything is written: if any
document's audit reports a loss, or fails to convert at all, this raises
before a single row is inserted, and the whole transaction rolls back. A
partial conversion must not be possible — a migration that raises takes the
application down with it, and that outage is the correct failure mode for a
lossy conversion, not silent corruption.

**`Migration` and `Audit` below are a frozen copy of
`scripts/migrate_resume_v2.py`, not an import of it — and the model classes
`Migration` builds are a frozen copy of the relevant slice of
`services/document/dtos/resume_object.py`, not an import of that either.**
This is deliberate, not a DRY lapse: `CLAUDE.md` exempts `alembic/versions/*`
from its object-oriented rules on the understanding that a migration is a
historical snapshot, and a snapshot that imports live code stops being one —
a future edit to either module would silently change what this
already-applied migration *would have done*, and a fresh database built from
scratch would diverge from a migrated one. Do not "fix" this by importing
the real modules.

The frozen model tree below is deliberately narrower than the real one: it
declares only the fields `Migration` actually populates. Fields the v1
source has no counterpart for (`volunteer`, `awards`, `publications`,
`languages`, `interests`, `references`, `education[].courses`,
`projects[].highlights/keywords/roles`, `specifics[].tech`) are simply never
declared, so they never appear in the dump — the same end state
`scripts/migrate_resume_v2.py`'s `Audit.drop_invented` reaches by popping
them back out after the fact. Since `Audit.scalars()` only ever counts
non-null leaf scalars, an empty list contributes nothing either way; the two
approaches are equivalent for audit purposes and this one is simpler to keep
correct in a frozen file.

Revision ID: b0baccd6558a
Revises: bcc6cfe85c34
Create Date: 2026-09-03 08:56:41.827169

"""
from collections import Counter
from datetime import UTC, datetime
from typing import Any, ClassVar, Sequence, Union

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b0baccd6558a"
down_revision: Union[str, Sequence[str], None] = "bcc6cfe85c34"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REVISION_NOTE = "Migrated to JSON Resume v2 (alembic b0baccd6558a)"
RESUME_TYPE = "resume"


# --------------------------------------------------------------------------
# Frozen v2 model tree — see module docstring.
# --------------------------------------------------------------------------


class _Base(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )


class _Withholdable(_Base):
    publish: bool = True


class _Location(_Withholdable):
    label: str | None = None
    kind: str | None = None
    note: str | None = None


class _Profile(_Withholdable):
    network: str | None = None
    url: str | None = None


class _Basics(_Base):
    name: str
    label: str | None = None
    tagline: str | None = None
    summary: str | None = None
    email: str | None = None
    phone: str | None = None
    location: _Location | None = None
    additional_locations: list[_Location] = []
    profiles: list[_Profile] = []


class _Role(_Base):
    title: str
    start_date: str | None = None
    end_date: str | None = None


class _ViaEmployer(_Base):
    name: str
    start_date: str | None = None
    end_date: str | None = None
    engagement: str | None = None


class _Metric(_Base):
    figure: str
    amount: str
    basis: str


class _Specific(_Base):
    detail: str


class _Highlight(_Withholdable):
    id: str
    summary: str
    specifics: list[_Specific] = []
    tech: list[str] = []
    metrics: list[_Metric] = []
    story: str | None = None


class _Work(_Withholdable):
    name: str
    url: str | None = None
    location: str | None = None
    description: str | None = None
    position: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    highlights: list[_Highlight] = []
    roles: list[_Role] = []
    role_location: str | None = None
    via_employer: _ViaEmployer | None = None


class _Education(_Withholdable):
    institution: str | None = None
    area: str | None = None
    study_type: str | None = None
    end_date: str | None = None
    location: str | None = None
    url: str | None = None


class _Certificate(_Withholdable):
    name: str
    url: str | None = None
    identifier: str | None = None


class _Keyword(_Withholdable):
    name: str
    url: str | None = None
    level: str | None = None
    last_used: str | None = None


class _Skill(_Withholdable):
    name: str
    keywords: list[_Keyword] = []


class _Project(_Withholdable):
    name: str | None = None
    url: str | None = None
    description: str | None = None


class _Narrative(_Base):
    career_arc: str | None = None
    current_status: str | None = None
    motivation: str | None = None
    looking_for: str | None = None
    avoiding: str | None = None
    working_style: str | None = None
    strengths: str | None = None
    voice: str | None = None


class _Logistics(_Base):
    work_authorization: str | None = None
    salary_expectation: str | None = None
    availability: str | None = None
    relocation: str | None = None
    on_site: str | None = None


class _FineTuningData(_Base):
    narrative: _Narrative | None = None
    logistics: _Logistics | None = None


class _ResumePrivateV2(_Base):
    basics: _Basics
    work: list[_Work] = []
    education: list[_Education] = []
    certificates: list[_Certificate] = []
    skills: list[_Skill] = []
    projects: list[_Project] = []
    fine_tuning_data: _FineTuningData | None = None


# --------------------------------------------------------------------------
# Migration — frozen copy of scripts/migrate_resume_v2.py's Migration class,
# rebuilt against the frozen model tree above instead of the live one.
# --------------------------------------------------------------------------


class Migration:
    """The v1 -> v2 field mapping, exactly as `ResumePrivate.MIGRATION`
    records it. One instance converts one document's `data`."""

    def __init__(self, old: dict[str, Any]) -> None:
        self.old = old

    def build(self) -> dict[str, Any]:
        resume = _ResumePrivateV2(
            basics=self._basics(),
            work=[self._work(job) for job in self.old["jobs"]],
            education=[self._education(e) for e in self.old["education"]],
            certificates=[self._certificate(c) for c in self.old["certifications"]],
            skills=[self._skill(g) for g in self.old["skill_groups"]],
            projects=[self._project(p) for p in self.old["personal_projects"]],
            fine_tuning_data=self._fine_tuning(),
        )
        return resume.model_dump(by_alias=True, exclude_none=True)

    def _basics(self) -> _Basics:
        contact = self.old["contact"]
        locations = [self._location(loc) for loc in contact["locations"]]
        return _Basics(
            name=self.old["profile"]["name"],
            label=self.old["profile"]["title"],
            tagline=self.old["profile"]["tagline"],
            summary=self.old["summary"],
            email=contact["email_address"],
            phone=contact["mobile_number"],
            location=locations[0] if locations else None,
            additional_locations=locations[1:],
            profiles=[self._profile(link) for link in contact["links"]],
        )

    def _location(self, old: dict[str, Any]) -> _Location:
        return _Location(
            label=old["label"],
            kind=old["kind"],
            note=old["note"],
            publish=old["publish"],
        )

    def _profile(self, old: dict[str, Any]) -> _Profile:
        return _Profile(network=old["label"], url=old["url"], publish=old["publish"])

    def _skill(self, old: dict[str, Any]) -> _Skill:
        return _Skill(
            name=old["name"],
            publish=old["publish"],
            keywords=[self._keyword(s) for s in old["skills"]],
        )

    def _keyword(self, old: dict[str, Any]) -> _Keyword:
        return _Keyword(
            name=old["name"],
            url=old["url"],
            level=old["level"],
            last_used=old["last_used"],
            publish=old["publish"],
        )

    def _certificate(self, old: dict[str, Any]) -> _Certificate:
        return _Certificate(
            name=old["name"],
            identifier=old["id"],
            url=old["url"],
            publish=old["publish"],
        )

    def _work(self, old: dict[str, Any]) -> _Work:
        roles = [self._role(r) for r in old["roles"]]
        return _Work(
            name=old["company"],
            url=old["company_url"],
            location=old["company_location"],
            description=old["description"],
            start_date=old["start"],
            end_date=old["end"],
            position=roles[0].title if roles else None,
            roles=roles,
            role_location=old["role_location"],
            via_employer=self._via_employer(old.get("via_employer")),
            highlights=[self._highlight(h) for h in old["highlights"]],
            publish=old["publish"],
        )

    def _role(self, old: dict[str, Any]) -> _Role:
        return _Role(title=old["title"], start_date=old["start"], end_date=old["end"])

    def _via_employer(self, old: dict[str, Any] | None) -> _ViaEmployer | None:
        if old is None:
            return None
        return _ViaEmployer(
            name=old["name"],
            start_date=old["start"],
            end_date=old["end"],
            engagement=old["engagement"],
        )

    def _highlight(self, old: dict[str, Any]) -> _Highlight:
        return _Highlight(
            id=old["id"],
            summary=old["summary"],
            specifics=[_Specific(detail=s) for s in old["specifics"]],
            tech=old["tech"],
            metrics=[_Metric(**m) for m in old["metrics"]],
            story=old.get("story"),
            publish=old["publish"],
        )

    def _education(self, old: dict[str, Any]) -> _Education:
        return _Education(
            institution=old["institution"],
            study_type=old["credential"],
            area=old["field"],
            end_date=old["year"],
            location=old["location"],
            url=old["url"],
            publish=old["publish"],
        )

    def _project(self, old: dict[str, Any]) -> _Project:
        return _Project(
            name=old["name"],
            url=old["link"],
            description=old["description"],
            publish=old["publish"],
        )

    def _fine_tuning(self) -> _FineTuningData:
        old = self.old.get("fine_tuning_data") or {}
        narrative = old.get("narrative")
        logistics = old.get("logistics")
        return _FineTuningData(
            narrative=_Narrative(**narrative) if narrative else None,
            logistics=_Logistics(**logistics) if logistics else None,
        )


class Audit:
    """Every non-null scalar in the source, counted, against the result.

    Frozen copy of `scripts/migrate_resume_v2.py`'s `Audit`, minus
    `drop_invented` — not needed here, see the module docstring for why.
    """

    def __init__(self, old: dict[str, Any], new: dict[str, Any]) -> None:
        self.old = old
        self.new = new

    def scalars(self, node: Any) -> Counter:
        found: Counter = Counter()
        if isinstance(node, dict):
            for value in node.values():
                found += self.scalars(value)
        elif isinstance(node, list):
            for value in node:
                found += self.scalars(value)
        elif node is not None:
            found[(type(node).__name__, node)] += 1
        return found

    def expected_extra(self) -> Counter:
        """The only new scalars a correct conversion introduces:
        `work[].position` mirrors `roles[0].title`, once per job that has a
        role."""
        extra: Counter = Counter()
        for job in self.old["jobs"]:
            if job["roles"]:
                extra[("str", job["roles"][0]["title"])] += 1
        return extra

    def expected_missing(self) -> Counter:
        """`contact.email_address`/`.mobile_number` and
        `fine_tuning_data.email_address`/`.mobile_number` both map to the
        same `basics` field, so a source that states the same value in both
        places loses one occurrence of it on purpose. A source where the two
        disagree is a real conflict, not a duplicate — this only excuses the
        value when both sides actually agree, so that case still shows up as
        a loss."""
        expected: Counter = Counter()
        contact = self.old["contact"]
        fine_tuning = self.old.get("fine_tuning_data") or {}
        for contact_key, fine_tuning_key in (
            ("email_address", "email_address"),
            ("mobile_number", "mobile_number"),
        ):
            value = contact.get(contact_key)
            if value is not None and fine_tuning.get(fine_tuning_key) == value:
                expected[("str", value)] += 1
        return expected

    def unexplained(self) -> tuple[Counter, Counter]:
        before = self.scalars(self.old)
        after = self.scalars(self.new)
        missing = before - after
        extra = after - before
        return (
            missing - self.expected_missing(),
            extra - self.expected_extra(),
        )

    def describe(self, unexplained_missing: Counter, unexplained_extra: Counter) -> str:
        lines = []
        if unexplained_missing:
            lines.append(f"  LOST {sum(unexplained_missing.values())} value(s):")
            lines.extend(
                f"    x{count} ({kind}) {value!r}"
                for (kind, value), count in unexplained_missing.most_common()
            )
        if unexplained_extra:
            lines.append(
                f"  UNEXPLAINED NEW {sum(unexplained_extra.values())} value(s):"
            )
            lines.extend(
                f"    x{count} ({kind}) {value!r}"
                for (kind, value), count in unexplained_extra.most_common()
            )
        return "\n".join(lines)


class ConversionFailure(Exception):
    """One document could not be converted cleanly. Raised, never swallowed
    — the caller collects every failure before raising once, so a deploy
    log names every offending document in one shot rather than stopping at
    the first."""


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


def _latest_resume_documents(conn: sa.Connection, table: sa.Table) -> list[Any]:
    """One row per (created_by, name) resume document: its highest
    revision_id."""
    rows = conn.execute(
        sa.select(table).where(table.c.type == RESUME_TYPE)
    ).fetchall()
    latest: dict[tuple[int, str], Any] = {}
    for row in rows:
        key = (row.created_by, row.name)
        current = latest.get(key)
        if current is None or row.revision_id > current.revision_id:
            latest[key] = row
    return list(latest.values())


def _report_untouched_metadata_and_skill(conn: sa.Connection, table: sa.Table) -> None:
    """Informational only — Phase 5 (the next revision) is what actually
    converts these; this just names what is left for it to do."""
    rows = conn.execute(
        sa.select(table).where(table.c.type.in_(("metadata", "skill")))
    ).fetchall()
    latest: dict[tuple[int, str], Any] = {}
    for row in rows:
        key = (row.created_by, row.name)
        current = latest.get(key)
        if current is None or row.revision_id > current.revision_id:
            latest[key] = row
    if latest:
        print(
            f"{len(latest)} metadata/skill document(s) left on schema 1 "
            "(a later migration converts these)."
        )


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    table = _documents_table()

    candidates = _latest_resume_documents(conn, table)
    to_convert = [row for row in candidates if "basics" not in row.data]
    already_v2 = len(candidates) - len(to_convert)
    print(
        f"{len(candidates)} resume document(s) found; {already_v2} already v2, "
        f"{len(to_convert)} to convert."
    )

    conversions: list[tuple[Any, dict[str, Any]]] = []
    failures: list[str] = []
    for row in to_convert:
        label = f"user={row.created_by} name={row.name!r}"
        try:
            new_data = Migration(row.data).build()
        except Exception as error:  # noqa: BLE001 - reported, then re-raised
            failures.append(f"{label}: FAILED to convert — {type(error).__name__}: {error}")
            continue

        unexplained_missing, unexplained_extra = Audit(row.data, new_data).unexplained()
        if unexplained_missing or unexplained_extra:
            failures.append(
                f"{label}: audit reported a loss:\n"
                + Audit(row.data, new_data).describe(unexplained_missing, unexplained_extra)
            )
            continue

        conversions.append((row, new_data))

    if failures:
        raise ConversionFailure(
            "Refusing to convert any resume document: "
            f"{len(failures)} of {len(to_convert)} failed audit or conversion.\n"
            + "\n".join(failures)
        )

    now = datetime.now(UTC).replace(tzinfo=None)
    for row, new_data in conversions:
        conn.execute(
            table.insert().values(
                created_by=row.created_by,
                name=row.name,
                revision_id=row.revision_id + 1,
                type=RESUME_TYPE,
                public=row.public,
                created_at=now,
                revision_note=REVISION_NOTE,
                data=new_data,
                schema_version=2,
            )
        )
        print(f"user={row.created_by} name={row.name!r}: wrote revision {row.revision_id + 1}.")

    _report_untouched_metadata_and_skill(conn, table)


def downgrade() -> None:
    """Downgrade schema.

    Deletes exactly the revisions this migration appended — identified by
    `schema_version = 2` *and* `revision_note = REVISION_NOTE`, not by
    `schema_version` alone, which would also match a revision a user wrote
    through the API after the deploy and destroy real work. Refuses if a
    newer revision now sits above one of the marked rows: removing a
    revision from under newer ones would leave the history claiming a
    lineage that never existed, same spirit as `35808d4a4c1d`'s downgrade
    refusing on a primary-key collision.
    """
    conn = op.get_bind()
    table = _documents_table()

    marked = conn.execute(
        sa.select(table).where(
            table.c.schema_version == 2, table.c.revision_note == REVISION_NOTE
        )
    ).fetchall()

    blocked = []
    for row in marked:
        newer = conn.execute(
            sa.select(sa.func.count()).select_from(table).where(
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
