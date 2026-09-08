"""One-time import of a personal job-hunt spreadsheet into application
tracking.

    PYTHONPATH=. uv run python -m scripts.import_job_hunt \\
        --username tristan --csv "data/2026 Job Hunt - Sheet1.csv"
    PYTHONPATH=. uv run python -m scripts.import_job_hunt \\
        --username tristan --csv "data/2026 Job Hunt - Sheet1.csv" --write

Dry run by default; `--write` is required to touch the database, and the
whole import then happens in one transaction, committed once — a partial
import is worse than none, and this is a one-time script on a personal
database.

Idempotent: re-running finds each row's application already there (matched
on company, normalized job title, and submission date) and skips it, so a
second run against the same file writes nothing new and exits 0.

Company names are imported verbatim - `"Bank of Oklahoma?"`,
`"JobTogether -> Unknown (Avive, duplicate)"` and a trailing space are kept
exactly as given, with a warning printed so they are easy to find and fix by
hand afterward. See section 10 of APPLICATION_TRACKING_PLAN.md for the full
column mapping and the response-to-status table this implements.
"""

import argparse
import asyncio
import csv
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from persistence.application import Application
from persistence.application_event import ApplicationEvent
from persistence.base import Clock
from persistence.company import Company
from persistence.user import User
from services.auth.principal import CredentialKind, Principal
from services.database.database_service import DatabaseService
from services.tracking.application_service import ApplicationService
from services.tracking.enums import ApplicationStatus
from services.tracking.normalization import Normalizer


class DateParser:
    """`M/D/YY` or `M/D/YYYY`. A two-digit year `NN` maps to `20NN`.
    Anything else - `"Unknown"`, a stray extra digit - is left unparsed
    rather than guessed at."""

    _PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$"
    )

    @classmethod
    def parse(cls, raw: str) -> date | None:
        match = cls._PATTERN.match(raw.strip())
        if match is None:
            return None
        month, day, year = match.groups()
        year_number = int(year) + 2000 if len(year) == 2 else int(year)
        try:
            return date(year_number, int(month), int(day))
        except ValueError:
            return None


class ResponseMapper:
    """The `Response` column, mapped to an `ApplicationStatus`. Case
    insensitive and whitespace stripped - see section 10.3."""

    _INTERVIEW: ClassVar[re.Pattern[str]] = re.compile(
        r"^interview\s+\d+$", re.IGNORECASE
    )
    _EMPTY_VALUES: ClassVar[frozenset[str]] = frozenset({"", "none"})
    _FOLLOW_UP_VALUES: ClassVar[frozenset[str]] = frozenset(
        {"follup", "follow up", "followup"}
    )

    @dataclass
    class Result:
        status: str | None
        description_prefix: str | None
        unmapped: bool

    @classmethod
    def map(cls, raw: str) -> Result:
        stripped = raw.strip()
        lowered = stripped.casefold()
        if lowered in cls._EMPTY_VALUES:
            return cls.Result(status=None, description_prefix=None, unmapped=False)
        if lowered == "rejected":
            return cls.Result(
                status=ApplicationStatus.REJECTED.value,
                description_prefix=None,
                unmapped=False,
            )
        if cls._INTERVIEW.match(stripped):
            return cls.Result(
                status=ApplicationStatus.INTERVIEW_COMPLETED.value,
                description_prefix=None,
                unmapped=False,
            )
        if lowered in cls._FOLLOW_UP_VALUES:
            return cls.Result(
                status=ApplicationStatus.FOLLOW_UP.value,
                description_prefix=None,
                unmapped=False,
            )
        return cls.Result(
            status=ApplicationStatus.FOLLOW_UP.value,
            description_prefix=f"Response: {stripped}. ",
            unmapped=True,
        )


class ImportWorker:
    """Reads the CSV, resolves companies, creates applications and events,
    and prints a summary. One instance per run."""

    SUSPICIOUS_MARKERS: ClassVar[tuple[str, ...]] = ("?", "->", "/", "(")
    EXPECTED_COLUMNS: ClassVar[frozenset[str]] = frozenset(
        {
            "Company",
            "Url",
            "Job Title",
            "Resume Used",
            "Submission Date",
            "Source",
            "System",
            "Response",
            "Response Date",
            "Reason",
        }
    )

    def __init__(self, user: User, csv_path: Path, write: bool) -> None:
        self.user = user
        self.csv_path = csv_path
        self.write = write
        self.principal = Principal(
            user_id=user.id,
            username=user.username,
            scopes=frozenset(),
            credential=CredentialKind.API_KEY,
        )
        self.companies_created = 0
        self.applications_created = 0
        self.events_created = 0
        self.skipped_existing = 0
        self._companies: dict[str, Company] = {}

    def _warn(self, row_number: int, company: str, message: str) -> None:
        print(f'  WARN row {row_number} "{company}": {message}')

    def _read_rows(self) -> list[dict[str, str]]:
        with self.csv_path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            missing = self.EXPECTED_COLUMNS - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"CSV is missing column(s): {sorted(missing)}")
            return list(reader)

    async def _resolve_company(
        self, db: AsyncSession, row_number: int, name: str, url: str
    ) -> Company:
        if any(marker in name for marker in self.SUSPICIOUS_MARKERS):
            self._warn(row_number, name, "name kept verbatim, review after import")

        normalized = Normalizer.company_name(name)
        company = self._companies.get(normalized)
        if company is None:
            company = await Company.get_by_normalized_name(db, self.user.id, normalized)

        if company is None:
            company = Company(
                user_id=self.user.id,
                name=name,
                normalized_name=normalized,
                website=url or None,
            )
            db.add(company)
            await db.flush()
            self.companies_created += 1
        elif company.website is None and url:
            company.website = url

        self._companies[normalized] = company
        return company

    def _parse_submission_date(
        self, row_number: int, company: str, raw: str
    ) -> date | None:
        if not raw:
            return None
        parsed = DateParser.parse(raw)
        if parsed is None:
            self._warn(
                row_number, company, f'Submission Date "{raw}" unparseable, left blank'
            )
        return parsed

    def _occurred_at(
        self,
        row_number: int,
        company: str,
        raw_response_date: str,
        date_submitted: date | None,
    ) -> datetime:
        if raw_response_date:
            parsed = DateParser.parse(raw_response_date)
            if parsed is not None:
                return datetime.combine(parsed, datetime.min.time())
            self._warn(
                row_number,
                company,
                f'Response Date "{raw_response_date}" unparseable, using Submission Date',
            )
        if date_submitted is not None:
            return datetime.combine(date_submitted, datetime.min.time())
        return Clock.utcnow()

    @staticmethod
    def _description(prefix: str | None, reason: str) -> str | None:
        if prefix and reason:
            return prefix + reason
        if prefix:
            return prefix.strip()
        return reason or None

    async def _find_existing_application(
        self,
        db: AsyncSession,
        company_id: int,
        normalized_job_title: str | None,
        date_submitted: date | None,
    ) -> Application | None:
        for application in await Application.list_for_company(
            db, self.user.id, company_id
        ):
            if (
                application.normalized_job_title == normalized_job_title
                and application.date_submitted == date_submitted
            ):
                return application
        return None

    async def _process_row(
        self,
        db: AsyncSession,
        application_service: ApplicationService,
        row_number: int,
        row: dict[str, str],
    ) -> None:
        name = row["Company"].strip()
        if not name:
            return

        url = row["Url"].strip()
        company = await self._resolve_company(db, row_number, name, url)

        job_title = row["Job Title"].strip() or None
        normalized_job_title = Normalizer.job_title(job_title)
        date_submitted = self._parse_submission_date(
            row_number, name, row["Submission Date"].strip()
        )

        existing = await self._find_existing_application(
            db, company.id, normalized_job_title, date_submitted
        )
        if existing is not None:
            self.skipped_existing += 1
            return

        application = Application(
            user_id=self.user.id,
            company_id=company.id,
            url=url or None,
            job_title=job_title,
            normalized_job_title=normalized_job_title,
            resume_label=row["Resume Used"].strip() or None,
            date_submitted=date_submitted,
            manually_modified=False,
            modification_note=f"Imported from {self.csv_path.name}",
            source=row["Source"].strip() or None,
            system=row["System"].strip() or None,
            status=ApplicationStatus.SUBMITTED.value,
        )
        db.add(application)
        await db.flush()
        self.applications_created += 1

        response = ResponseMapper.map(row["Response"].strip())
        reason = row["Reason"].strip()
        if response.unmapped:
            self._warn(
                row_number,
                name,
                f"unmapped Response {row['Response'].strip()!r}, using follow_up",
            )
        if response.status is None and not reason:
            return

        event = ApplicationEvent(
            user_id=self.user.id,
            application_id=application.id,
            status=response.status,
            description=self._description(response.description_prefix, reason),
            occurred_at=self._occurred_at(
                row_number, name, row["Response Date"].strip(), date_submitted
            ),
        )
        db.add(event)
        await db.flush()
        self.events_created += 1
        await application_service._recompute_status(application)

    async def run(self) -> int:
        rows = self._read_rows()
        print(f"{len(rows)} data rows read from {self.csv_path}")

        async with DatabaseService.session() as db:
            application_service = ApplicationService(db, self.principal)
            # Row 1 is the header; the first data row is row 2, matching the
            # spreadsheet's own line numbers rather than a 1-based data index.
            for row_number, row in enumerate(rows, start=2):
                await self._process_row(db, application_service, row_number, row)

            verb = "created" if self.write else "would be created"
            print(
                f"{self.companies_created} companies, {self.applications_created} "
                f"applications, {self.events_created} events {verb}; "
                f"{self.skipped_existing} skipped as existing"
            )
            if self.write:
                await db.commit()
                print("Wrote everything in one transaction.")
            else:
                await db.rollback()
                print("Dry run: nothing was written. Pass --write to write.")
        return 0


class Cli:
    DEFAULT_CSV: ClassVar[str] = "data/2026 Job Hunt - Sheet1.csv"

    @staticmethod
    def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
        parser = argparse.ArgumentParser(
            prog="python -m scripts.import_job_hunt",
            description=__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        parser.add_argument(
            "--username", required=True, help="the account to import applications into"
        )
        parser.add_argument(
            "--csv", default=Cli.DEFAULT_CSV, help="path to the job-hunt CSV export"
        )
        parser.add_argument(
            "--write",
            action="store_true",
            help="write the import; without this flag nothing is written",
        )
        return parser.parse_args(argv)

    async def run(self, argv: list[str] | None = None) -> int:
        args = self._parse_args(argv)

        async with DatabaseService.session() as db:
            user = await User.get_user_by_username(db, args.username)
        if user is None:
            print(f"No user named {args.username!r} exists.")
            return 1

        csv_path = Path(args.csv)
        if not csv_path.is_file():
            print(f"No CSV file at {csv_path}.")
            return 1

        return await ImportWorker(user, csv_path, args.write).run()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(Cli().run()))
