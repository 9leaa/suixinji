"""Small, opt-in SQLAlchemy query counters for benchmark and regression tests."""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from sqlalchemy import Engine, event


@dataclass
class QueryStats:
    """Aggregate SQL activity observed during one measurement window."""

    count: int = 0
    failed: int = 0
    total_duration_ms: float = 0.0
    durations_ms: list[float] = field(default_factory=list)

    def observe(self, duration_ms: float, *, failed: bool = False) -> None:
        self.count += 1
        self.failed += int(failed)
        self.total_duration_ms += duration_ms
        self.durations_ms.append(duration_ms)

    def to_dict(self) -> dict[str, Any]:
        ordered = sorted(self.durations_ms)

        def percentile(ratio: float) -> float | None:
            if not ordered:
                return None
            index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
            return round(ordered[index], 3)

        return {
            "count": self.count,
            "failed": self.failed,
            "total_duration_ms": round(self.total_duration_ms, 3),
            "p50_duration_ms": percentile(0.50),
            "p95_duration_ms": percentile(0.95),
            "p99_duration_ms": percentile(0.99),
        }


@contextmanager
def capture_sql_queries(engine: Engine) -> Iterator[QueryStats]:
    """Capture SQL executions on ``engine`` without changing application behavior.

    This is intentionally opt-in. Production paths do not pay for SQL text
    collection, while repository tests and benchmark runs can assert query
    budgets and latency regressions.
    """

    stats = QueryStats()
    started: dict[int, float] = {}

    def before_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        started[id(context)] = time.perf_counter()

    def after_cursor_execute(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        begin = started.pop(id(context), None)
        stats.observe((time.perf_counter() - begin) * 1000 if begin is not None else 0.0)

    def handle_error(exception_context: Any) -> None:
        context = exception_context.execution_context
        begin = started.pop(id(context), None) if context is not None else None
        stats.observe((time.perf_counter() - begin) * 1000 if begin is not None else 0.0, failed=True)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    event.listen(engine, "after_cursor_execute", after_cursor_execute)
    event.listen(engine, "handle_error", handle_error)
    try:
        yield stats
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
        event.remove(engine, "after_cursor_execute", after_cursor_execute)
        event.remove(engine, "handle_error", handle_error)
