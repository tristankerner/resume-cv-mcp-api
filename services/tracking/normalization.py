import re
import unicodedata
from typing import ClassVar


class Normalizer:
    """Turns free-form text into the dedup key it is compared under.

    Every method here is pure and stateless: same input, same output, no
    database. `DuplicateFinder` and every tracking service that needs a
    `normalized_*` column call straight into this rather than repeating any
    of it.
    """

    LEGAL_SUFFIXES: ClassVar[frozenset[str]] = frozenset(
        {
            "inc",
            "incorporated",
            "llc",
            "l l c",
            "ltd",
            "limited",
            "corp",
            "corporation",
            "co",
            "company",
            "plc",
            "gmbh",
            "sa",
            "srl",
            "bv",
            "ag",
            "pty",
            "group",
            "holdings",
        }
    )

    _SENIORITY_TOKENS: ClassVar[frozenset[str]] = frozenset(
        {
            "senior",
            "sr",
            "staff",
            "lead",
            "principal",
            "junior",
            "jr",
            "i",
            "ii",
            "iii",
            "iv",
        }
    )

    _NON_ALNUM_SPACE: ClassVar[re.Pattern[str]] = re.compile(r"[^a-z0-9 ]")
    _WHITESPACE: ClassVar[re.Pattern[str]] = re.compile(r"\s+")

    @classmethod
    def _fold(cls, value: str) -> str:
        """Unicode NFKD normalize, strip combining marks, casefold."""
        decomposed = unicodedata.normalize("NFKD", value)
        without_marks = "".join(
            ch for ch in decomposed if not unicodedata.combining(ch)
        )
        return without_marks.casefold()

    @classmethod
    def _clean(cls, value: str) -> str:
        """Fold, strip to `[a-z0-9 ]`, collapse whitespace."""
        folded = cls._fold(value)
        stripped = cls._NON_ALNUM_SPACE.sub("", folded)
        return cls._WHITESPACE.sub(" ", stripped).strip()

    @classmethod
    def _strip_legal_suffixes(cls, tokens: list[str]) -> list[str]:
        """Drop trailing tokens found in `LEGAL_SUFFIXES`, repeatedly, without
        ever emptying the result — a suffix phrase spanning several tokens
        (`"l l c"`) is checked as a whole, longest phrase first."""
        by_length = sorted(
            (suffix.split(" ") for suffix in cls.LEGAL_SUFFIXES),
            key=len,
            reverse=True,
        )
        while True:
            for suffix_tokens in by_length:
                width = len(suffix_tokens)
                if len(tokens) > width and tokens[-width:] == suffix_tokens:
                    tokens = tokens[:-width]
                    break
            else:
                return tokens

    @classmethod
    def company_name(cls, value: str) -> str:
        folded = cls._fold(value).replace("&", " and ")
        cleaned = cls._WHITESPACE.sub(" ", cls._NON_ALNUM_SPACE.sub("", folded)).strip()
        if not cleaned:
            return cls._WHITESPACE.sub(" ", cls._fold(value)).strip()
        tokens = cls._strip_legal_suffixes(cleaned.split(" "))
        return " ".join(tokens)

    @classmethod
    def stack_item(cls, value: str) -> str:
        return cls._clean(value)

    @classmethod
    def job_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = cls._clean(value)
        if not cleaned:
            return cleaned
        kept = [
            token for token in cleaned.split(" ") if token not in cls._SENIORITY_TOKENS
        ]
        return " ".join(kept)

    # A job code shorter than this, once punctuation is gone, is not evidence
    # of anything: "1" or "12" collides constantly and would flag unrelated
    # applications as the same requisition. Such a code is still stored and
    # still displayed — it just does not participate in matching.
    JOB_CODE_MIN_MATCH_LENGTH: ClassVar[int] = 3

    _NON_ALNUM: ClassVar[re.Pattern[str]] = re.compile(r"[^A-Z0-9]")

    @classmethod
    def job_code(cls, value: str | None) -> str | None:
        """A requisition code reduced to the characters that identify it.

        Upper-cased with every separator removed, so `REQ-12345`,
        `req 12345` and `Req_12345` are one code — which is the whole point:
        two recruiters submitting the same requisition rarely spell it the
        same way. Returns None for a code with nothing alphanumeric in it, or
        one too short to mean anything (`JOB_CODE_MIN_MATCH_LENGTH`), so a
        useless key never lands in the column that matching reads.
        """
        cleaned = cls.job_code_key(value)
        if cleaned is None or len(cleaned) < cls.JOB_CODE_MIN_MATCH_LENGTH:
            return None
        return cleaned

    @classmethod
    def job_code_key(cls, value: str | None) -> str | None:
        """`job_code` without the minimum-length rule, for filtering.

        A search for a two-character code has to match the rows that store
        one — of which there are none, since `job_code` refuses to store a key
        that short. Reusing `job_code` here would turn that search into `None`
        and quietly drop the filter, answering "which applications have code
        X?" with every application the caller owns.
        """
        if value is None:
            return None
        folded = unicodedata.normalize("NFKD", value).upper()
        cleaned = cls._NON_ALNUM.sub("", folded)
        return cleaned or None

    @classmethod
    def person_name(cls, first: str | None, last: str | None) -> str | None:
        joined = " ".join(part for part in (first, last) if part).strip()
        cleaned = cls._clean(joined)
        return cleaned or None

    @classmethod
    def email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().casefold()
        return cleaned or None
