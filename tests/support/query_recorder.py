import re
import time
from collections import Counter
from re import Pattern
from types import TracebackType
from typing import Any, ClassVar, Self

from sqlalchemy import event
from sqlalchemy.engine import Engine

from services.database.database_service import DatabaseService


class QueryRecorder:
    """Every statement issued through the sync engine backing
    `DatabaseService`'s async engine, for the life of one `with` block.

    `after_cursor_execute` is the hook rather than counting at the session
    level: it fires once per round trip regardless of which layer issued the
    statement, so it is the same number Neon would bill for. The listener is
    removed in `__exit__` — one left attached across a loop of `with` blocks
    quietly multiplies every count after it, which is exactly the bug this
    class exists to catch, not repeat.
    """

    _TABLE: ClassVar[Pattern[str]] = re.compile(
        r"\b(?:FROM|INTO|UPDATE)\s+\"?(\w+)\"?", re.IGNORECASE
    )

    def __init__(self, delay_seconds: float = 0.0) -> None:
        self.delay_seconds = delay_seconds
        self.statements: list[str] = []
        self._engine: Engine = DatabaseService.engine().sync_engine

    def __enter__(self) -> Self:
        event.listen(self._engine, "after_cursor_execute", self._record)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        event.remove(self._engine, "after_cursor_execute", self._record)

    def _record(
        self,
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        self.statements.append(statement)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)

    def tally(self) -> Counter[str]:
        """Statement counts grouped by leading verb and table, so N
        identical per-row lookups collapse into one line instead of N."""
        counts: Counter[str] = Counter()
        for statement in self.statements:
            verb = statement.strip().split(None, 1)[0].upper()
            match = self._TABLE.search(statement)
            table = match.group(1) if match else "?"
            counts[f"{verb} {table}"] += 1
        return counts

    def count(self) -> int:
        return len(self.statements)
