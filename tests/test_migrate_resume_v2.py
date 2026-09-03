"""Unit coverage for the converter in `scripts/migrate_resume_v2.py`, against
a small fictional v1 payload — never against real data, per
`V2_MIGRATION_PLAN.md`'s rule that `data/` never supplies a test fixture.
"""

from scripts.migrate_resume_v2 import Audit, Migration


def v1_payload() -> dict:
    """A minimal but structurally complete v1 `ResumePrivate`, exercising one
    of everything `Migration` maps: a second job for `via_employer` and
    multi-role progression, a skill, a certification, education, and a
    personal project."""
    return {
        "profile": {"name": "Sam Fictional", "title": "Engineer", "tagline": "tagline"},
        "contact": {
            "locations": [
                {"label": "Remote", "kind": "remote", "note": "note", "publish": True},
                {"label": "Metro", "kind": "metro", "note": "note2", "publish": False},
            ],
            "links": [
                {"label": "site", "url": "https://example.invalid", "publish": True}
            ],
            "email_address": "sam@example.invalid",
            "mobile_number": "555-0100",
        },
        "summary": "summary",
        "skill_groups": [
            {
                "name": "group",
                "publish": True,
                "skills": [
                    {
                        "name": "Python",
                        "url": None,
                        "level": "expert",
                        "last_used": "2026",
                        "publish": True,
                    }
                ],
            }
        ],
        "certifications": [{"name": "Cert", "id": "C-1", "url": None, "publish": True}],
        "jobs": [
            {
                "company": "Company",
                "company_url": None,
                "company_location": "Remote",
                "start": "2020-01",
                "end": None,
                "via_employer": None,
                "description": "description",
                "role_location": "Remote",
                "roles": [{"title": "Engineer", "start": "2020", "end": None}],
                "highlights": [
                    {
                        "id": "highlight-1",
                        "summary": "did a thing",
                        "specifics": ["detail one", "detail two"],
                        "tech": ["Python"],
                        "metrics": [
                            {"figure": "figure", "amount": "1", "basis": "basis"}
                        ],
                        "story": "story",
                        "publish": True,
                    }
                ],
                "publish": True,
            },
            {
                "company": "Agency Co",
                "company_url": None,
                "company_location": "Remote",
                "start": "2018-01",
                "end": "2019-12",
                "via_employer": {
                    "name": "Staffing Group",
                    "start": "2018-01",
                    "end": "2018-04",
                    "engagement": "contract-to-hire",
                },
                "description": "description2",
                "role_location": "Remote",
                "roles": [
                    {"title": "Engineer II", "start": "2018-06", "end": "2019-12"},
                    {
                        "title": "Engineer II (Contract)",
                        "start": "2018-01",
                        "end": "2018-06",
                    },
                ],
                "highlights": [],
                "publish": True,
            },
        ],
        "education": [
            {
                "institution": "Fictional State",
                "credential": "B.S.",
                "field": "CS",
                "year": "2018",
                "location": "TX",
                "url": None,
                "publish": True,
            }
        ],
        "personal_projects": [
            {
                "name": "project",
                "link": "https://example.invalid/project",
                "description": "description3",
                "publish": False,
            }
        ],
        "fine_tuning_data": {
            "email_address": "sam@example.invalid",
            "mobile_number": "555-0100",
            "narrative": {"career_arc": "arc"},
            "logistics": {"work_authorization": "citizen"},
        },
    }


class TestMigration:
    def test_builds_a_valid_resume_private(self):
        result = Migration(v1_payload()).build()
        assert result.basics.name == "Sam Fictional"
        assert result.basics.email == "sam@example.invalid"
        assert result.basics.location is not None
        assert result.basics.location.label == "Remote"
        assert [loc.label for loc in result.basics.additional_locations] == ["Metro"]

    def test_multi_role_progression_is_kept_and_position_mirrors_the_latest(self):
        agency_job = Migration(v1_payload()).build().work[1]
        assert [role.title for role in agency_job.roles] == [
            "Engineer II",
            "Engineer II (Contract)",
        ]
        assert agency_job.position == "Engineer II"

    def test_via_employer_is_kept(self):
        agency_job = Migration(v1_payload()).build().work[1]
        assert agency_job.via_employer is not None
        assert agency_job.via_employer.name == "Staffing Group"

    def test_specifics_become_objects_with_no_invented_tech(self):
        highlight = Migration(v1_payload()).build().work[0].highlights[0]
        assert [s.detail for s in highlight.specifics] == ["detail one", "detail two"]
        assert all(s.tech == [] for s in highlight.specifics)


class TestAudit:
    def test_a_correct_conversion_reports_no_loss_after_pruning(self):
        old = v1_payload()
        new = Migration(old).build()
        dumped = new.model_dump(by_alias=True, exclude_none=True)
        audit = Audit(old, dumped)
        pruned = audit.drop_invented(dumped)
        report = Audit(old, pruned).report("test")
        assert report.clean
        assert report.lost == 0

    def test_a_dropped_value_is_caught(self):
        old = v1_payload()
        new = Migration(old).build()
        dumped = new.model_dump(by_alias=True, exclude_none=True)
        dumped["basics"]["summary"] = "tampered"
        report = Audit(old, dumped).report("test")
        assert not report.clean
        assert report.lost > 0

    def test_drop_invented_removes_the_empty_v1_less_sections(self):
        old = v1_payload()
        new = Migration(old).build()
        dumped = new.model_dump(by_alias=True, exclude_none=True)
        pruned = Audit(old, dumped).drop_invented(dumped)
        assert "volunteer" not in pruned
        assert "awards" not in pruned
        assert "courses" not in pruned["education"][0]
        for work in pruned["work"]:
            for highlight in work.get("highlights", []):
                for specific in highlight.get("specifics", []):
                    assert "tech" not in specific
