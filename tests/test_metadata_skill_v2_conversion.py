"""Regression tests for the metadata/skill v1 -> v2 converter in
`alembic/versions/78480d80f589_convert_metadata_and_skill_to_v2.py`.

The first deploy of that migration aborted on real data after passing every
check against the fictional examples: the real documents are roughly two and
a half times larger and reference paths in forms the example-derived mapping
table never contained. These tests exist so that failure mode is caught here
rather than against the production database.

Two tiers:

- Always-on tests, using fixtures built in this file, that pin the behaviours
  the deploy failure exposed — prefix matching, `[]` arity, comma-joined
  values, non-path values, and the residual check.
- A real-document test that runs the operator's own metadata and skill
  documents through the converter, skipped when they are absent. Those files
  live in `examples/` but are gitignored (`.gitignore`), so this test runs on
  the machine that has them and skips in CI. It reads them and asserts on
  counts and shapes; it never copies their content anywhere.
"""

import importlib.util
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION = (
    REPO_ROOT / "alembic/versions/78480d80f589_convert_metadata_and_skill_to_v2.py"
)
SNAPSHOTS = MIGRATION.parent / "schema_snapshots"


def _load_migration() -> Any:
    """Import the migration module directly.

    `alembic/versions` is not an importable package, and this deliberately
    does not go through `alembic.command`: these tests exercise the
    conversion logic, not the migration run.
    """
    spec = importlib.util.spec_from_file_location("_mig_78480d80f589", MIGRATION)
    assert spec is not None and spec.loader is not None, MIGRATION
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mig = _load_migration()


@pytest.fixture(scope="module")
def resolver():
    return mig.SchemaPathResolver()


@pytest.fixture(scope="module")
def v2_examples() -> dict[str, dict]:
    return {
        doc_type: json.loads((SNAPSHOTS / f"v2_{doc_type}.example.json").read_text())
        for doc_type in ("metadata", "skill")
    }


class TestPathRewriting:
    """Each case here is a form the real documents used and the original
    exact-match table did not contain."""

    @pytest.mark.parametrize(
        "v1, expected",
        [
            # Child of a mapped prefix — the table had the prefix only.
            ("resume.contact.locations[].label", "resume.basics.location.label"),
            ("resume.contact.locations[].kind", "resume.basics.location.kind"),
            # Arity changes: v1 locations[] is a list, v2 location is not.
            ("resume.contact.locations", "resume.basics.location"),
            # Bare section names — the table had only the `[]` spellings.
            ("resume.jobs", "resume.work"),
            ("resume.skill_groups", "resume.skills"),
            ("resume.certifications", "resume.certificates"),
            ("resume.personal_projects", "resume.projects"),
            ("resume.contact.links", "resume.basics.profiles"),
            # Trailing [] on a terminal scalar list is dropped.
            (
                "resume.jobs[].highlights[].tech[]",
                "resume.work[].highlights[].tech",
            ),
            # The section that dissolved into basics.
            ("resume.profile", "resume.basics"),
            ("resume.profile.title", "resume.basics.label"),
            # Ordinary renames still work.
            ("resume.summary", "resume.basics.summary"),
            (
                "resume.jobs[].highlights[].summary",
                "resume.work[].highlights[].summary",
            ),
        ],
    )
    def test_rewrites_to_a_resolvable_v2_path(self, v1, expected, resolver):
        rewritten = mig.PathRewriter.rewrite_path(v1)
        assert rewritten == expected
        assert resolver.resolves(rewritten)

    def test_comma_joined_paths_are_split_and_each_mapped(self, resolver):
        rewritten = mig.PathRewriter.rewrite_path(
            "resume.education, resume.certifications"
        )
        assert rewritten == "resume.education, resume.certificates"
        for part in rewritten.split(","):
            assert resolver.resolves(part.strip())

    @pytest.mark.parametrize(
        "value",
        [
            "header",
            "summary",
            "date",
            "addressee",
            "https://example.invalid/resume.json — work[].highlights[]",
        ],
    )
    def test_values_that_are_not_paths_are_left_untouched(self, value):
        assert mig.PathRewriter.rewrite_path(value) == value

    def test_an_unmapped_v1_path_still_fails_resolution(self, resolver):
        """Prefix matching must not make an unknown path silently 'work'."""
        assert not resolver.resolves(
            mig.PathRewriter.rewrite_path("resume.invented_section[].nope")
        )


class TestProseRewriting:
    def test_prose_paths_are_substituted(self):
        out = mig.PathRewriter.rewrite_prose(
            "Read resume.jobs[].highlights[].summary for every job."
        )
        assert "resume.work[].highlights[].summary" in out
        assert "resume.jobs" not in out

    def test_bare_v1_identifiers_in_prose_are_substituted(self):
        out = mig.PathRewriter.rewrite_prose(
            "fine_tuning_data.logistics is background only; trim personal_projects."
        )
        assert "fineTuningData" in out
        assert "fine_tuning_data" not in out
        assert "personal_projects" not in out

    def test_prose_in_a_list_is_substituted(self):
        """The bug that made an earlier fix report clean while leaving 13
        stale references: list recursion returned strings untouched."""
        rewritten = mig.DocumentRewriter().rewrite(
            {"rules": ["never cite resume.jobs[].highlights[].metrics"]}
        )
        assert "resume.work[].highlights[].metrics" in rewritten["rules"][0]
        assert not mig._residual_v1_references(rewritten)

    def test_prose_fields_nobody_enumerated_are_still_substituted(self):
        """`rule`, `condition`, `never_claim`, `structure` and `cautions` all
        carried v1 references in the real documents and were in no prose
        allowlist. The denylist design is what covers them."""
        document = {
            "guardrails": [{"rule": "never invent resume.jobs[].highlights[].metrics"}],
            "escalations": [{"condition": "resume.contact.locations is empty"}],
            "cover_letter": {
                "never_claim": ["anything absent from resume.fine_tuning_data"],
                "structure": ["open with resume.jobs[].highlights[].story"],
            },
            "cautions": ["fine_tuning_data.logistics is judgement-only"],
        }
        assert (
            mig._residual_v1_references(mig.DocumentRewriter().rewrite(document)) == []
        )


class TestResidualDetection:
    def test_a_surviving_v1_reference_is_reported(self):
        residual = mig._residual_v1_references(
            {"guardrails": [{"rule": "cite resume.jobs[].highlights[].metrics"}]}
        )
        assert len(residual) == 1
        assert "guardrails[0].rule" in residual[0]

    def test_a_clean_document_reports_nothing(self, v2_examples):
        for document in v2_examples.values():
            assert mig._residual_v1_references(document) == []

    def test_a_resume_json_url_is_not_mistaken_for_a_v1_path(self):
        assert mig._residual_v1_references({"x": "https://e.invalid/resume.json"}) == []


class TestFieldClassification:
    @pytest.mark.parametrize("field", ["never_publish", "sources"])
    def test_fields_that_carry_paths_are_classified_as_paths(self, field):
        """Both were missing, so their v1 entries were carried through
        verbatim — `sources` silently, since it is unioned rather than
        replaced."""
        assert field in mig.PATH_FIELD_NAMES

    def test_sources_entries_are_rewritten(self, resolver):
        rewritten = mig.DocumentRewriter().rewrite(
            {
                "keywords": {
                    "sources": ["job_posting", "resume.skill_groups[].skills[].name"]
                }
            }
        )
        sources = rewritten["keywords"]["sources"]
        assert sources[0] == "job_posting"
        assert sources[1] == "resume.skills[].keywords[].name"
        assert resolver.resolves(sources[1])


class TestRealDocuments:
    """The documents the failed deploy actually choked on.

    Gitignored, so absent in CI — skipped rather than failed there.
    """

    PATHS: ClassVar[dict[str, Path]] = {
        "metadata": REPO_ROOT / "examples/resume.metadata.json",
        "skill": REPO_ROOT / "examples/resume.skill.json",
    }

    @pytest.fixture(params=["metadata", "skill"])
    def real_document(self, request):
        path = self.PATHS[request.param]
        if not path.exists():
            pytest.skip(f"{path.name} is gitignored and not present")
        return request.param, json.loads(path.read_text())

    def _convert(self, doc_type: str, data: dict, v2_examples: dict) -> dict:
        converter = mig.DocumentTypeConverter(
            doc_type,
            json.loads((SNAPSHOTS / f"v1_{doc_type}.seeded.json").read_text()),
            v2_examples[doc_type],
        )
        assert not converter.is_untouched(data), (
            "expected the real document to be customised, so the merge branch "
            "is what these tests exercise"
        )
        return converter.merge(data)

    def test_converts_with_no_unresolved_paths(self, real_document, v2_examples):
        doc_type, data = real_document
        converted = self._convert(doc_type, data, v2_examples)
        assert mig._unresolved_paths(converted) == []

    def test_converts_with_no_surviving_v1_references(self, real_document, v2_examples):
        doc_type, data = real_document
        converted = self._convert(doc_type, data, v2_examples)
        assert mig._residual_v1_references(converted) == []

    def test_passes_its_own_merge_audit(self, real_document, v2_examples):
        doc_type, data = real_document
        converter = mig.DocumentTypeConverter(
            doc_type,
            json.loads((SNAPSHOTS / f"v1_{doc_type}.seeded.json").read_text()),
            v2_examples[doc_type],
        )
        converted = converter.merge(data)
        assert converter.audit_merge(data, converted) is None

    def test_result_validates_against_the_live_model(self, real_document, v2_examples):
        from services.document.dtos.resume_object import ResumeMetadata
        from services.document.dtos.resume_skill import ResumeSkill

        doc_type, data = real_document
        converted = self._convert(doc_type, data, v2_examples)
        {"metadata": ResumeMetadata, "skill": ResumeSkill}[doc_type].model_validate(
            converted
        )
