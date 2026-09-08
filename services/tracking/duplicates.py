import re
from difflib import SequenceMatcher
from typing import ClassVar

from services.tracking.dtos.common import DuplicateCandidate, MatchKind


class DuplicateFinder:
    """Ranks near-matches for a candidate name against a haystack of existing
    rows, using only the standard library — see section 5.3 of the tracking
    plan for why `difflib` and not a trigram extension.
    """

    FUZZY_THRESHOLD: ClassVar[float] = 0.85
    MAX_RESULTS: ClassVar[int] = 5

    _WHOLE_TOKEN: ClassVar[re.Pattern[str]] = re.compile(r"(?:^|\s)%s(?:$|\s)")

    @classmethod
    def _contains_whole_token(cls, shorter: str, longer: str) -> bool:
        if not shorter:
            return False
        pattern = re.compile(rf"(?:^|\s){re.escape(shorter)}(?:$|\s)")
        return pattern.search(longer) is not None

    @classmethod
    def _match(cls, needle: str, normalized: str) -> tuple[MatchKind, float] | None:
        if needle == normalized:
            return "exact", 1.0

        shorter, longer = (
            (needle, normalized)
            if len(needle) <= len(normalized)
            else (normalized, needle)
        )
        if longer and cls._contains_whole_token(shorter, longer):
            return "contains", len(shorter) / len(longer)

        ratio = SequenceMatcher(None, needle, normalized).ratio()
        if ratio >= cls.FUZZY_THRESHOLD:
            return "fuzzy", ratio
        return None

    @classmethod
    def rank(
        cls,
        needle: str,
        haystack: list[tuple[int, str, str]],
        limit: int | None = None,
    ) -> list[DuplicateCandidate]:
        """`haystack` is `(id, display_name, normalized_name)`. `needle` is
        already normalized — every caller normalizes before ranking.

        `limit` overrides `MAX_RESULTS` for callers that need a different cap
        - `TrackingTools.search_companies` takes its own `limit` argument
        rather than the fixed five a duplicate check refuses at.
        """
        candidates: list[DuplicateCandidate] = []
        for entity_id, display_name, normalized in haystack:
            found = cls._match(needle, normalized)
            if found is None:
                continue
            match, score = found
            candidates.append(
                DuplicateCandidate(
                    id=entity_id, label=display_name, match=match, score=score
                )
            )
        candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        return candidates[: limit if limit is not None else cls.MAX_RESULTS]
