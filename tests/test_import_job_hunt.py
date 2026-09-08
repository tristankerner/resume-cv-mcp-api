"""scripts/import_job_hunt.py: dry-run by default, idempotent, dialect-
portable date/response parsing. Uses a small fixture CSV rather than the
real (gitignored) export - see section 10.7 of the tracking plan."""

from pathlib import Path

from sqlalchemy import select

from persistence.application import Application
from persistence.application_event import ApplicationEvent
from persistence.company import Company
from scripts.import_job_hunt import Cli
from services.database.database_service import DatabaseService

HEADER = (
    "Company,Url,Job Title,Resume Used,Submission Date,Source,System,"
    "Response,Response Date,Reason"
)

CSV_ROWS = [
    HEADER,
    # A clean row: no response yet.
    (
        "Acme Inc,https://acme.example,Software Engineer,Resume v1,6/1/26,"
        "LinkedIn,AshbyHQ,None,,"
    ),
    # A second row for the same company - reuses the company, fills no new
    # website (already set), and gets its own rejected event.
    (
        "Acme Inc,,Backend Engineer,Resume v1,6/2/26,LinkedIn,AshbyHQ,Rejected,"
        "6/10/26,Not a fit."
    ),
    # An unparseable submission date.
    "Bad Dates Co,,QA Engineer,Resume v1,not-a-date,LinkedIn,AshbyHQ,None,,",
    # Response=Interview 1.
    (
        "Interview Co,,Engineer,Resume v1,6/3/26,LinkedIn,AshbyHQ,Interview 1,"
        "6/12/26,Went well."
    ),
    # An unmapped, non-empty response value.
    "Weird Response Co,,Engineer,Resume v1,6/4/26,LinkedIn,AshbyHQ,Ghosted by them,,",
]


def _write_csv(path: Path, rows: list[str]) -> Path:
    csv_path = path / "job_hunt.csv"
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return csv_path


async def _counts() -> tuple[int, int, int]:
    async with DatabaseService.session() as db:
        companies = len((await db.execute(select(Company))).scalars().all())
        applications = len((await db.execute(select(Application))).scalars().all())
        events = len((await db.execute(select(ApplicationEvent))).scalars().all())
    return companies, applications, events


class TestDryRun:
    async def test_writes_nothing(self, admin, tmp_path, capsys):
        csv_path = _write_csv(tmp_path, CSV_ROWS)
        exit_code = await Cli().run(
            ["--username", admin.username, "--csv", str(csv_path)]
        )
        assert exit_code == 0
        assert await _counts() == (0, 0, 0)

        output = capsys.readouterr().out
        assert "5 data rows read" in output
        assert "would be created" in output
        assert "Dry run: nothing was written" in output

    async def test_reports_the_unparseable_date(self, admin, tmp_path, capsys):
        csv_path = _write_csv(tmp_path, CSV_ROWS)
        await Cli().run(["--username", admin.username, "--csv", str(csv_path)])
        output = capsys.readouterr().out
        assert (
            'WARN row 4 "Bad Dates Co": Submission Date "not-a-date" unparseable'
            in output
        )

    async def test_reports_the_unmapped_response(self, admin, tmp_path, capsys):
        csv_path = _write_csv(tmp_path, CSV_ROWS)
        await Cli().run(["--username", admin.username, "--csv", str(csv_path)])
        output = capsys.readouterr().out
        assert 'WARN row 6 "Weird Response Co": unmapped Response' in output


class TestWrite:
    async def test_creates_companies_applications_and_events(
        self, admin, tmp_path, capsys
    ):
        csv_path = _write_csv(tmp_path, CSV_ROWS)
        exit_code = await Cli().run(
            ["--username", admin.username, "--csv", str(csv_path), "--write"]
        )
        assert exit_code == 0

        companies, applications, events = await _counts()
        assert companies == 4  # Acme Inc reused across its two rows
        assert applications == 5
        # Acme(rejected) + Interview Co(interview_completed) = 2 events.
        # The "None" row and the unparseable-date row have no response and
        # no reason, so they get no event; the unmapped-response row has a
        # non-empty Response and so does get one.
        assert events == 3

        output = capsys.readouterr().out
        assert "Wrote everything in one transaction." in output

    async def test_reuses_the_company_and_only_fills_a_missing_website(
        self, admin, tmp_path
    ):
        csv_path = _write_csv(tmp_path, CSV_ROWS)
        await Cli().run(
            ["--username", admin.username, "--csv", str(csv_path), "--write"]
        )
        async with DatabaseService.session() as db:
            rows = (
                (await db.execute(select(Company).where(Company.name == "Acme Inc")))
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].website == "https://acme.example"

    async def test_derived_status_is_recomputed_from_the_event(self, admin, tmp_path):
        csv_path = _write_csv(tmp_path, CSV_ROWS)
        await Cli().run(
            ["--username", admin.username, "--csv", str(csv_path), "--write"]
        )
        async with DatabaseService.session() as db:
            rejected = (
                (
                    await db.execute(
                        select(Application).where(
                            Application.job_title == "Backend Engineer"
                        )
                    )
                )
                .scalars()
                .first()
            )
            interviewed = (
                (
                    await db.execute(
                        select(Application)
                        .where(Application.job_title == "Engineer")
                        .where(Application.source == "LinkedIn")
                    )
                )
                .scalars()
                .all()
            )
        assert rejected is not None
        assert rejected.status == "rejected"
        statuses = {app.status for app in interviewed}
        assert "interview_completed" in statuses

    async def test_rerun_is_idempotent(self, admin, tmp_path, capsys):
        csv_path = _write_csv(tmp_path, CSV_ROWS)
        await Cli().run(
            ["--username", admin.username, "--csv", str(csv_path), "--write"]
        )
        first_counts = await _counts()

        exit_code = await Cli().run(
            ["--username", admin.username, "--csv", str(csv_path), "--write"]
        )
        assert exit_code == 0
        assert await _counts() == first_counts

        output = capsys.readouterr().out
        assert "5 skipped as existing" in output
        assert "0 companies, 0 applications, 0 events created" in output


class TestCliErrors:
    async def test_unknown_username_is_an_error(self, tmp_path):
        csv_path = _write_csv(tmp_path, CSV_ROWS)
        exit_code = await Cli().run(
            ["--username", "nobody-by-this-name", "--csv", str(csv_path)]
        )
        assert exit_code == 1

    async def test_missing_csv_file_is_an_error(self, admin, tmp_path):
        exit_code = await Cli().run(
            ["--username", admin.username, "--csv", str(tmp_path / "nope.csv")]
        )
        assert exit_code == 1
