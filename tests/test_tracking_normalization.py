"""Pure logic: Normalizer and DuplicateFinder. No database."""

import pytest

from services.tracking.duplicates import DuplicateFinder
from services.tracking.normalization import Normalizer


class TestCompanyName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Yudrio, Inc", "yudrio"),
            ("Liberty Personnel Services, Inc.", "liberty personnel services"),
            ("Recruiting from Scratch ", "recruiting from scratch"),
            ("VVDN / Inseego", "vvdn inseego"),
            ("Bank of Oklahoma?", "bank of oklahoma"),
            ("binti", "binti"),
            ("Binti", "binti"),
            ("Plaid", "plaid"),
            ("Plaid Inc.", "plaid"),
            ("Acme & Sons", "acme and sons"),
            ("Acme L L C", "acme"),
            ("Acme LLC", "acme"),
            ("Group", "group"),
        ],
    )
    def test_known_values(self, raw, expected):
        assert Normalizer.company_name(raw) == expected

    def test_binti_and_capitalized_binti_match(self):
        assert Normalizer.company_name("binti") == Normalizer.company_name("Binti")

    def test_suffix_stripping_never_empties_the_result(self):
        """A name that IS a legal suffix keeps its last token rather than
        normalizing to the empty string."""
        assert Normalizer.company_name("Group") == "group"
        assert Normalizer.company_name("LLC") == "llc"

    def test_unicode_is_folded(self):
        assert Normalizer.company_name("Café Corp") == Normalizer.company_name(
            "Cafe Corp"
        )

    def test_falls_back_to_casefolded_original_when_nothing_survives(self):
        """Nothing in "???" is [a-z0-9 ], so step 6's suffix stripping never
        runs; step 7's fallback is the casefolded original, punctuation and
        all - not an empty string."""
        assert Normalizer.company_name("???") == "???"


class TestStackItem:
    def test_no_suffix_stripping(self):
        # "Go" is not a legal suffix, but this proves stack_item does not run
        # the suffix step at all - a name that happens to equal one survives.
        assert Normalizer.stack_item("Group") == "group"

    def test_basic_cleaning(self):
        assert Normalizer.stack_item("PostgreSQL!") == "postgresql"
        assert Normalizer.stack_item("  Go  ") == "go"


class TestJobTitle:
    def test_none_stays_none(self):
        assert Normalizer.job_title(None) is None

    def test_seniority_prefix_dropped(self):
        assert Normalizer.job_title("Senior Software Engineer") == Normalizer.job_title(
            "Software Engineer"
        )

    def test_seniority_suffix_dropped(self):
        assert Normalizer.job_title("Software Engineer II") == Normalizer.job_title(
            "Software Engineer"
        )

    def test_staff_and_principal_dropped(self):
        assert Normalizer.job_title("Staff Engineer") == Normalizer.job_title(
            "Principal Engineer"
        )

    def test_unrelated_titles_still_differ(self):
        assert Normalizer.job_title("Backend Engineer") != Normalizer.job_title(
            "Frontend Engineer"
        )


class TestPersonName:
    def test_both_parts(self):
        assert Normalizer.person_name("Sam", "Okafor") == "sam okafor"

    def test_only_first(self):
        assert Normalizer.person_name("Sam", None) == "sam"

    def test_only_last(self):
        assert Normalizer.person_name(None, "Okafor") == "okafor"

    def test_both_none_is_none(self):
        assert Normalizer.person_name(None, None) is None

    def test_both_empty_is_none(self):
        assert Normalizer.person_name("", "") is None


class TestEmail:
    def test_casefolds_and_strips(self):
        assert Normalizer.email("  Sam@Example.COM  ") == "sam@example.com"

    def test_none_stays_none(self):
        assert Normalizer.email(None) is None

    def test_empty_becomes_none(self):
        assert Normalizer.email("   ") is None


class TestDuplicateFinderExactAndContains:
    def test_exact_match(self):
        results = DuplicateFinder.rank("plaid", [(12, "Plaid", "plaid")])
        assert len(results) == 1
        assert results[0].match == "exact"
        assert results[0].score == 1.0
        assert results[0].id == 12

    def test_contains_match_scores_by_length_ratio(self):
        results = DuplicateFinder.rank(
            "plaid", [(47, "Plaid Payments", "plaid payments")]
        )
        assert len(results) == 1
        assert results[0].match == "contains"
        assert results[0].score == pytest.approx(len("plaid") / len("plaid payments"))

    def test_contains_requires_whole_token_boundary(self):
        """ "plaid" is not "contained" in "plaidx" under the whole-token rule -
        that match, if any, has to come from the fuzzy fallback instead."""
        results = DuplicateFinder.rank("plaid", [(1, "Plaidx", "plaidx")])
        assert not any(r.match == "contains" for r in results)

    def test_dissimilar_short_strings_are_not_a_candidate(self):
        results = DuplicateFinder.rank("plaid", [(1, "Zzz", "zzz")])
        assert results == []

    def test_fuzzy_match_above_threshold(self):
        results = DuplicateFinder.rank("plaid inc", [(1, "Plaid Inc", "plaid inc")])
        assert results[0].match == "exact"

        results = DuplicateFinder.rank("plaidd", [(1, "Plaid", "plaid")])
        assert len(results) == 1
        assert results[0].match == "fuzzy"
        assert results[0].score >= DuplicateFinder.FUZZY_THRESHOLD

    def test_below_threshold_is_not_a_candidate(self):
        results = DuplicateFinder.rank(
            "plaid", [(1, "Totally Different Co", "totally different co")]
        )
        assert results == []

    def test_results_sorted_descending_and_capped_at_five(self):
        haystack = [(i, f"Plaid {i}", f"plaid {i}") for i in range(10)]
        haystack.append((99, "Plaid", "plaid"))
        results = DuplicateFinder.rank("plaid", haystack)
        assert len(results) == DuplicateFinder.MAX_RESULTS
        assert results[0].id == 99
        assert results[0].match == "exact"
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_empty_haystack(self):
        assert DuplicateFinder.rank("plaid", []) == []


class TestJobCodeNormalization:
    """Two recruiters rarely spell a requisition the same way; the normalized
    form is what makes them one code."""

    def test_separators_and_case_are_ignored(self):
        for spelling in (
            "REQ-12345",
            "req 12345",
            "Req_12345",
            "req/12345",
            "REQ12345",
        ):
            assert Normalizer.job_code(spelling) == "REQ12345", spelling

    def test_accents_are_folded(self):
        assert Normalizer.job_code("réq-12345") == Normalizer.job_code("req-12345")

    def test_a_code_too_short_to_mean_anything_is_not_a_key(self):
        # Stored and displayed, but never matched on - "1" would collide with
        # every other one-character code.
        assert Normalizer.job_code("1") is None
        assert Normalizer.job_code("A-2") is None
        assert Normalizer.job_code("ABC") == "ABC"

    def test_a_code_with_nothing_alphanumeric_is_none(self):
        assert Normalizer.job_code("---") is None
        assert Normalizer.job_code("") is None
        assert Normalizer.job_code(None) is None

    def test_search_key_keeps_short_codes(self):
        """`job_code_key` has no minimum, so searching for a short code
        matches the rows that store one - of which there are none - rather
        than collapsing to None and dropping the filter."""
        assert Normalizer.job_code_key("A-2") == "A2"
        assert Normalizer.job_code_key("---") is None
