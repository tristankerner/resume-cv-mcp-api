"""convert resume documents to v3

Converts every stored resume document whose latest revision still has a
string-shaped `projects[].highlights[]` entry, by appending a new revision —
never rewriting one, matching `b0baccd6558a`'s reasoning (`documents` carries
a no-UPDATE trigger).

Unlike `b0baccd6558a`, this migration needs no frozen shadow Pydantic model
tree: v2 -> v3 renames nothing and restructures exactly one thing — a project
highlight string becomes `{"id": ..., "summary": ..., "publish": true}` — so
the conversion mutates the stored dict directly rather than rebuilding the
whole document through a parallel model tree. Every other key is carried
through byte-identical.

Three mappings, exactly as MCP_WRITEBACK_PLAN.md §6.2 describes:

1. Each project-highlight string becomes an object. Its `id` is a slug
   derived from the project name and the highlight's index, checked for
   collision against every highlight id already in the document — across
   `work[]`, `volunteer[]`, and the projects already converted — with a
   numeric suffix appended on collision. An empty or whitespace-only string
   is dropped rather than kept as a highlight with an empty summary.
2. `fineTuningData.logistics.travel` is left absent, not guessed — there is
   no v2 field to map it from.
3. `schema_version` is stamped to 3 on the appended revision.

Audit, before any write: every non-null leaf scalar in the document is
counted, before and after. A converted document must have exactly as many as
it started with, plus 2 per converted highlight (its generated `id` and its
`publish: true`), minus 1 per dropped empty string. A mismatch raises and the
whole transaction rolls back before any row is written — same all-or-nothing
shape as `b0baccd6558a`.

Metadata and skill documents are not converted: their models did not change,
so a document stored at v2 still validates against the v3 model, and the
catalogue's v3 example is what a *new* account is seeded with — an existing
account's metadata and skill documents are the owner's own edits and must not
be overwritten with an example.

Revision ID: d980e94de5a2
Revises: 6ec9cebeb712
Create Date: 2026-09-09 00:15:00.000000

"""
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any, Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d980e94de5a2"
down_revision: Union[str, Sequence[str], None] = "6ec9cebeb712"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REVISION_NOTE = "Migrated to resume schema v3 (alembic d980e94de5a2)"
RESUME_TYPE = "resume"
SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")


class HighlightSlugger:
    """One project's worth of generated ids, collision-checked against every
    highlight id already in the document — see the module docstring."""

    def __init__(self, existing_ids: set[str]) -> None:
        self.existing_ids = existing_ids

    def slug_for(self, project_name: str | None, index: int) -> tuple[str, bool]:
        """Returns (id, collided) — `collided` is true when the first-choice
        slug was already taken and a numeric suffix had to be appended."""
        base = SLUG_NON_ALNUM.sub("-", (project_name or "project").lower()).strip("-")
        candidate = f"proj-{base or 'project'}-{index + 1}"
        unique = candidate
        suffix = 2
        while unique in self.existing_ids:
            unique = f"{candidate}-{suffix}"
            suffix += 1
        self.existing_ids.add(unique)
        return unique, unique != candidate


class ProjectHighlightConverter:
    """Converts one resume document's `data`, in isolation from the others.

    Frozen scope — see the module docstring for why this does not rebuild
    the document through a shadow model tree the way `b0baccd6558a` does.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def needs_conversion(self) -> bool:
        for project in self.data.get("projects") or []:
            for highlight in project.get("highlights") or []:
                if isinstance(highlight, str):
                    return True
        return False

    def _existing_highlight_ids(self) -> set[str]:
        ids: set[str] = set()
        for section in ("work", "volunteer", "projects"):
            for entry in self.data.get(section) or []:
                for highlight in entry.get("highlights") or []:
                    if isinstance(highlight, dict) and "id" in highlight:
                        ids.add(highlight["id"])
        return ids

    def convert(self) -> tuple[dict[str, Any], int, int, int]:
        """Returns (new_data, highlights_converted, empty_strings_dropped,
        id_collisions_suffixed)."""
        slugger = HighlightSlugger(self._existing_highlight_ids())
        converted = 0
        dropped = 0
        collisions = 0

        new_projects = []
        for project in self.data.get("projects") or []:
            highlights = project.get("highlights") or []
            if not any(isinstance(h, str) for h in highlights):
                new_projects.append(project)
                continue

            new_highlights = []
            for index, highlight in enumerate(highlights):
                if not isinstance(highlight, str):
                    new_highlights.append(highlight)
                    continue
                summary = highlight.strip()
                if not summary:
                    dropped += 1
                    continue
                new_id, collided = slugger.slug_for(project.get("name"), index)
                if collided:
                    collisions += 1
                new_highlights.append(
                    {"id": new_id, "summary": summary, "publish": True}
                )
                converted += 1

            new_projects.append({**project, "highlights": new_highlights})

        new_data = {**self.data, "projects": new_projects}
        return new_data, converted, dropped, collisions


class Audit:
    """Non-null leaf scalars, counted — frozen copy of `b0baccd6558a`'s
    `Audit.scalars`, reused as a total count rather than a value-by-value
    diff: the only transformations here are dropping an empty string and
    adding two known-shape scalars per surviving highlight, which a total
    count already proves out completely."""

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

    def total(self, node: Any) -> int:
        return sum(self.scalars(node).values())


class ConversionFailure(Exception):
    """One document could not be converted cleanly. Raised, never swallowed
    — see `b0baccd6558a`'s identical reasoning."""


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
    rows = conn.execute(sa.select(table).where(table.c.type == RESUME_TYPE)).fetchall()
    latest: dict[tuple[int, str], Any] = {}
    for row in rows:
        key = (row.created_by, row.name)
        current = latest.get(key)
        if current is None or row.revision_id > current.revision_id:
            latest[key] = row
    return list(latest.values())


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    table = _documents_table()
    audit = Audit()

    candidates = _latest_resume_documents(conn, table)
    to_convert = [
        row for row in candidates if ProjectHighlightConverter(row.data).needs_conversion()
    ]
    already_v3 = len(candidates) - len(to_convert)
    print(
        f"{len(candidates)} resume document(s) found; {already_v3} already v3, "
        f"{len(to_convert)} to convert."
    )

    conversions: list[tuple[Any, dict[str, Any], int, int]] = []
    failures: list[str] = []
    total_converted = 0
    total_dropped = 0
    total_collisions = 0

    for row in to_convert:
        label = f"user={row.created_by} name={row.name!r}"
        try:
            new_data, converted, dropped, collisions = ProjectHighlightConverter(
                row.data
            ).convert()
        except Exception as error:  # noqa: BLE001 - reported, then re-raised
            failures.append(
                f"{label}: FAILED to convert — {type(error).__name__}: {error}"
            )
            continue

        before = audit.total(row.data)
        after = audit.total(new_data)
        expected = before + 2 * converted - dropped
        if after != expected:
            failures.append(
                f"{label}: audit reported a loss — expected {expected} non-null "
                f"scalar(s) after conversion (before={before}, "
                f"+2x{converted} converted, -{dropped} dropped), found {after}."
            )
            continue

        conversions.append((row, new_data, converted, dropped))
        total_converted += converted
        total_dropped += dropped
        total_collisions += collisions

    if failures:
        raise ConversionFailure(
            "Refusing to convert any resume document: "
            f"{len(failures)} of {len(to_convert)} failed audit or conversion.\n"
            + "\n".join(failures)
        )

    now = datetime.now(UTC).replace(tzinfo=None)
    for row, new_data, _converted, _dropped in conversions:
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
                schema_version=3,
            )
        )
        print(
            f"user={row.created_by} name={row.name!r}: wrote revision "
            f"{row.revision_id + 1}."
        )

    print(
        f"{len(conversions)} document(s) converted; {total_converted} project "
        f"highlight(s) converted; {total_dropped} empty string(s) dropped; "
        f"{total_collisions} id collision(s) suffixed."
    )


def downgrade() -> None:
    """Downgrade schema.

    Deletes exactly the revisions this migration appended — identified by
    `schema_version = 3` *and* `revision_note = REVISION_NOTE`, not by
    `schema_version` alone. Refuses if a newer revision now sits above one
    of the marked rows, same reasoning as `b0baccd6558a`'s downgrade.
    """
    conn = op.get_bind()
    table = _documents_table()

    marked = conn.execute(
        sa.select(table).where(
            table.c.schema_version == 3, table.c.revision_note == REVISION_NOTE
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
            blocked.append(
                f"user={row.created_by} name={row.name!r} revision={row.revision_id}"
            )

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
