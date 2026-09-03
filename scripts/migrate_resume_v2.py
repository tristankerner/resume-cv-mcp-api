"""Offline v1 -> v2 resume migration, for every stored resume document.

Ports the conversion in `data/v2-migration/migrate.py` — the reference
implementation this migration was designed and validated against — and its
audit: the multiset of every non-null scalar in the source document, compared
against the same multiset in the converted result. A value that goes missing,
gets truncated, or is silently rewritten shows up as a difference rather than
being taken on trust. A document whose audit reports any loss is reported and
left untouched; nothing about this script writes a lossy conversion.

    PYTHONPATH=. uv run python -m scripts.migrate_resume_v2            # dry run
    PYTHONPATH=. uv run python -m scripts.migrate_resume_v2 --write    # writes new revisions

Dry run by default; `--write` is required to touch the database. Every write
is a new revision — see `Document.upsert_document` — never an UPDATE of a
stored row: `Document` is append-only, and revision identity is `data`, so
mutating a row in place would be indistinguishable from every other kind of
data loss this script exists to catch.
"""

import argparse
import asyncio
from collections import Counter
from typing import Any, ClassVar

from sqlalchemy import select

from persistence.document import Document
from services.database.database_service import DatabaseService
from services.document.document_types import DocumentType
from services.document.dtos.resume_object import (
    Basics,
    Certificate,
    Education,
    FineTuningData,
    Highlight,
    Keyword,
    Location,
    Logistics,
    Metric,
    Narrative,
    Profile,
    Project,
    ResumePrivate,
    Role,
    Skill,
    Specific,
    ViaEmployer,
    Work,
)


class Migration:
    """The v1 -> v2 field mapping, exactly as `ResumePrivate.MIGRATION`
    records it. One instance converts one document's `data`."""

    def __init__(self, old: dict[str, Any]) -> None:
        self.old = old

    def build(self) -> ResumePrivate:
        return ResumePrivate(
            basics=self._basics(),
            work=[self._work(job) for job in self.old["jobs"]],
            education=[self._education(e) for e in self.old["education"]],
            certificates=[self._certificate(c) for c in self.old["certifications"]],
            skills=[self._skill(g) for g in self.old["skill_groups"]],
            projects=[self._project(p) for p in self.old["personal_projects"]],
            fine_tuning_data=self._fine_tuning(),
        )

    def _basics(self) -> Basics:
        contact = self.old["contact"]
        locations = [self._location(loc) for loc in contact["locations"]]
        return Basics(
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

    def _location(self, old: dict[str, Any]) -> Location:
        return Location(
            label=old["label"],
            kind=old["kind"],
            note=old["note"],
            publish=old["publish"],
        )

    def _profile(self, old: dict[str, Any]) -> Profile:
        return Profile(network=old["label"], url=old["url"], publish=old["publish"])

    def _skill(self, old: dict[str, Any]) -> Skill:
        return Skill(
            name=old["name"],
            publish=old["publish"],
            keywords=[self._keyword(s) for s in old["skills"]],
        )

    def _keyword(self, old: dict[str, Any]) -> Keyword:
        return Keyword(
            name=old["name"],
            url=old["url"],
            level=old["level"],
            last_used=old["last_used"],
            publish=old["publish"],
        )

    def _certificate(self, old: dict[str, Any]) -> Certificate:
        return Certificate(
            name=old["name"],
            identifier=old["id"],
            url=old["url"],
            publish=old["publish"],
        )

    def _work(self, old: dict[str, Any]) -> Work:
        roles = [self._role(r) for r in old["roles"]]
        return Work(
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

    def _role(self, old: dict[str, Any]) -> Role:
        return Role(title=old["title"], start_date=old["start"], end_date=old["end"])

    def _via_employer(self, old: dict[str, Any] | None) -> ViaEmployer | None:
        if old is None:
            return None
        return ViaEmployer(
            name=old["name"],
            start_date=old["start"],
            end_date=old["end"],
            engagement=old["engagement"],
        )

    def _highlight(self, old: dict[str, Any]) -> Highlight:
        return Highlight(
            id=old["id"],
            summary=old["summary"],
            specifics=[Specific(detail=s) for s in old["specifics"]],
            tech=old["tech"],
            metrics=[Metric(**m) for m in old["metrics"]],
            story=old.get("story"),
            publish=old["publish"],
        )

    def _education(self, old: dict[str, Any]) -> Education:
        return Education(
            institution=old["institution"],
            study_type=old["credential"],
            area=old["field"],
            end_date=old["year"],
            location=old["location"],
            url=old["url"],
            publish=old["publish"],
        )

    def _project(self, old: dict[str, Any]) -> Project:
        return Project(
            name=old["name"],
            url=old["link"],
            description=old["description"],
            publish=old["publish"],
        )

    def _fine_tuning(self) -> FineTuningData:
        old = self.old.get("fine_tuning_data") or {}
        narrative = old.get("narrative")
        logistics = old.get("logistics")
        return FineTuningData(
            narrative=Narrative(**narrative) if narrative else None,
            logistics=Logistics(**logistics) if logistics else None,
        )


class Audit:
    """Every non-null scalar in the source, counted, against the result.

    The comparison the whole script exists for: a value dropped, truncated,
    or silently rewritten by `Migration` shows up as a difference here rather
    than being taken on trust.
    """

    # Fields the v1 document had no counterpart for, so `model_dump` writes
    # them as empty only because `ResumePrivate` defaults them — not because
    # the source said anything. Declaring them here is what keeps a genuinely
    # empty `volunteer: []` in the *source* (impossible in v1, but the
    # invariant is worth stating) from masking a real loss elsewhere.
    NO_V1_COUNTERPART: ClassVar[dict[str, tuple[str, ...]]] = {
        "": (
            "volunteer",
            "awards",
            "publications",
            "languages",
            "interests",
            "references",
        ),
        "education": ("courses",),
        "projects": ("highlights", "keywords", "roles"),
    }

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

    def report(self, prefix: str) -> AuditReport:
        before = self.scalars(self.old)
        after = self.scalars(self.new)

        missing = before - after
        extra = after - before
        unexplained_missing = missing - self.expected_missing()
        unexplained_extra = extra - self.expected_extra()

        lines = [
            f"{prefix}: {sum(before.values())} scalar(s) before, {sum(after.values())} after"
        ]
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
        if not unexplained_missing and not unexplained_extra:
            lines.append(
                "  clean: every source value survives, nothing unexplained added"
            )

        return AuditReport(
            lost=sum(unexplained_missing.values()),
            added=sum(unexplained_extra.values()),
            clean=not unexplained_missing and not unexplained_extra,
            lines=lines,
        )

    def drop_invented(self, new: dict[str, Any]) -> dict[str, Any]:
        """Prune the sections and fields `NO_V1_COUNTERPART` declares, and
        the specifics-level `tech` that is new in v2 and always empty coming
        out of a v1 source, so the written document is a mapping of the
        source rather than the source plus invented structure."""
        for section, fields in self.NO_V1_COUNTERPART.items():
            entries = [new] if section == "" else new.get(section, [])
            for entry in entries:
                for field in fields:
                    entry.pop(field, None)
        for work in new.get("work", []):
            for highlight in work.get("highlights", []):
                for specific in highlight.get("specifics", []):
                    specific.pop("tech", None)
        return new


class AuditReport:
    def __init__(self, lost: int, added: int, clean: bool, lines: list[str]) -> None:
        self.lost = lost
        self.added = added
        self.clean = clean
        self.lines = lines

    def print(self) -> None:
        for line in self.lines:
            print(line)


class ResumeMigrator:
    """Loads every stored resume document, converts it, audits the result,
    and — only with `--write`, and only when the audit is clean — writes a
    new revision."""

    def __init__(self, write: bool) -> None:
        self.write = write
        self.converted = 0
        self.skipped_already_v2 = 0
        self.refused_for_loss = 0
        self.failed = 0

    async def run(self) -> int:
        async with DatabaseService.session() as db:
            stmt = select(Document).where(Document.type == DocumentType.RESUME.value)
            documents = (await db.execute(stmt)).scalars().all()

        latest: dict[tuple[int, str], Document] = {}
        for document in documents:
            key = (document.created_by, document.name)
            current = latest.get(key)
            if current is None or document.revision_id > current.revision_id:
                latest[key] = document

        if not latest:
            print("No resume documents found.")
            return 0

        print(f"{len(latest)} resume document(s) found across every user.\n")
        for document in latest.values():
            # One unconvertible document must not abort the run. A v1 payload
            # missing a key `Migration` reads raises, and letting that
            # propagate would stop every document behind it — on a `--write`
            # run, halfway through, with no report of what was left. Each
            # document is independent and every write is its own revision, so
            # the useful behaviour is to record the failure and carry on.
            try:
                await self._migrate_one(document)
            except Exception as error:  # noqa: BLE001 - reported, not swallowed
                label = f"user={document.created_by} name={document.name!r}"
                print(f"{label}: FAILED to convert — {type(error).__name__}: {error}\n")
                self.failed += 1

        print(
            f"\n{self.converted} converted, {self.skipped_already_v2} already v2, "
            f"{self.refused_for_loss} refused for audit loss, {self.failed} failed."
        )
        if not self.write:
            print("Dry run: nothing was written. Pass --write to write new revisions.")
        return 1 if self.refused_for_loss or self.failed else 0

    async def _migrate_one(self, document: Document) -> None:
        label = f"user={document.created_by} name={document.name!r}"
        if "basics" in document.data:
            print(f"{label}: already v2, skipping.")
            self.skipped_already_v2 += 1
            return

        new = Migration(document.data).build()
        dumped = new.model_dump(by_alias=True, exclude_none=True)
        audit = Audit(document.data, dumped)
        pruned = audit.drop_invented(dumped)
        # Re-run the scalar count against the pruned document: dropping the
        # invented, always-empty fields must not itself look like a loss.
        report = Audit(document.data, pruned).report(label)
        report.print()

        if not report.clean:
            self.refused_for_loss += 1
            print(f"{label}: refusing to write — audit reported a loss.\n")
            return

        self.converted += 1
        if not self.write:
            print(f"{label}: dry run, not written.\n")
            return

        revision = Document(
            created_by=document.created_by,
            name=document.name,
            type=document.type,
            revision_note="Migrated to JSON Resume v2 (scripts/migrate_resume_v2.py)",
            data=pruned,
        )
        async with DatabaseService.session() as db:
            result = await Document.upsert_document(
                db, revision, public=document.public
            )
        if result.document is not None:
            print(f"{label}: wrote revision {result.document.revision_id}.\n")


class Cli:
    @staticmethod
    def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
        parser = argparse.ArgumentParser(
            prog="python -m scripts.migrate_resume_v2",
            description=__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        parser.add_argument(
            "--write",
            action="store_true",
            help="write new revisions; without this flag nothing is written",
        )
        return parser.parse_args(argv)

    async def run(self, argv: list[str] | None = None) -> int:
        args = self._parse_args(argv)
        return await ResumeMigrator(write=args.write).run()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(Cli().run()))
