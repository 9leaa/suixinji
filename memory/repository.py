"""SQLite repository for Memory V2."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

from core.settings import (
    MEMORY_CONSOLIDATION_RUN_LEASE_SECONDS,
    MEMORY_DB_BUSY_TIMEOUT_MS,
    MEMORY_DB_WRITE_MAX_ATTEMPTS,
    MEMORY_QUERY_MIN_SCORE,
)
from memory.models import (
    MEMORY_EXTRACTION_STATUSES,
    MEMORY_STATUSES,
    ConsolidationRun,
    MemoryCandidate,
    MemoryExtractionState,
    MemoryRecord,
    MemorySource,
    MemoryVersion,
    new_id,
    normalize_content,
    utc_now_iso,
)

DB_PATH = Path("data/memory/memory.db")
T = TypeVar("T")


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=max(MEMORY_DB_BUSY_TIMEOUT_MS, 1) / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {max(MEMORY_DB_BUSY_TIMEOUT_MS, 1)}")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _is_locked_error(exc: Exception) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).casefold()


def _run_write(operation: Callable[[], T], *, max_attempts: int | None = None) -> T:
    attempts = max(1, int(max_attempts or MEMORY_DB_WRITE_MAX_ATTEMPTS))
    delay = 0.05
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            if not _is_locked_error(exc) or attempt >= attempts:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable sqlite write retry state")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _is_stale(value: str | None, *, lease_seconds: int) -> bool:
    parsed = _parse_iso(value)
    if parsed is None:
        return True
    return (datetime.now().astimezone() - parsed).total_seconds() > lease_seconds


def init_db(db_path: str | Path | None = None) -> None:
    with _connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                space_id TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                content TEXT NOT NULL,
                normalized_content TEXT,
                importance REAL NOT NULL,
                confidence REAL NOT NULL,
                status TEXT NOT NULL,
                task_status TEXT,
                valid_from TEXT,
                valid_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_accessed_at TEXT,
                access_count INTEGER DEFAULT 0,
                current_version INTEGER DEFAULT 1
            );

            CREATE INDEX IF NOT EXISTS idx_memories_space_status
            ON memories(space_id, status, memory_type);

            CREATE TABLE IF NOT EXISTS memory_sources (
                memory_id TEXT NOT NULL,
                note_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(memory_id, note_id, relation)
            );

            CREATE INDEX IF NOT EXISTS idx_memory_sources_memory
            ON memory_sources(memory_id);

            CREATE TABLE IF NOT EXISTS memory_versions (
                id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                source_note_id TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_memory_versions_memory
            ON memory_versions(memory_id, version);

            CREATE TABLE IF NOT EXISTS memory_vectors (
                memory_id TEXT PRIMARY KEY,
                embedding_json TEXT,
                model TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memory_extraction_states (
                note_id TEXT PRIMARY KEY,
                space_id TEXT NOT NULL,
                status TEXT NOT NULL,
                candidate_count INTEGER NOT NULL DEFAULT 0,
                processed_count INTEGER NOT NULL DEFAULT 0,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                started_at TEXT,
                completed_at TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_memory_extraction_status
            ON memory_extraction_states(space_id, status, updated_at);

            CREATE TABLE IF NOT EXISTS memory_consolidation_runs (
                id TEXT PRIMARY KEY,
                space_id TEXT NOT NULL,
                cadence TEXT NOT NULL,
                period_key TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                error TEXT,
                result_json TEXT,
                UNIQUE(space_id, cadence, period_key)
            );

            CREATE INDEX IF NOT EXISTS idx_memory_consolidation_status
            ON memory_consolidation_runs(cadence, period_key, status);
            """
        )


def _memory_from_row(row: sqlite3.Row, *, sources: list[MemorySource] | None = None, versions: list[MemoryVersion] | None = None) -> MemoryRecord:
    return MemoryRecord(
        id=str(row["id"]),
        space_id=str(row["space_id"]),
        memory_type=str(row["memory_type"]),
        content=str(row["content"]),
        normalized_content=str(row["normalized_content"] or ""),
        importance=float(row["importance"]),
        confidence=float(row["confidence"]),
        status=str(row["status"]),
        task_status=row["task_status"],
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        last_accessed_at=row["last_accessed_at"],
        access_count=int(row["access_count"] or 0),
        current_version=int(row["current_version"] or 1),
        sources=sources or [],
        versions=versions or [],
    )


def _extraction_state_from_row(row: sqlite3.Row) -> MemoryExtractionState:
    return MemoryExtractionState(
        note_id=str(row["note_id"]),
        space_id=str(row["space_id"]),
        status=str(row["status"]),
        candidate_count=int(row["candidate_count"] or 0),
        processed_count=int(row["processed_count"] or 0),
        attempt_count=int(row["attempt_count"] or 0),
        last_error=row["last_error"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        updated_at=str(row["updated_at"]),
    )


def _consolidation_run_from_row(row: sqlite3.Row) -> ConsolidationRun:
    return ConsolidationRun(
        id=str(row["id"]),
        space_id=str(row["space_id"]),
        cadence=str(row["cadence"]),
        period_key=str(row["period_key"]),
        status=str(row["status"]),
        started_at=str(row["started_at"]),
        completed_at=row["completed_at"],
        error=row["error"],
        result_json=row["result_json"],
    )


def _load_sources(conn: sqlite3.Connection, memory_id: str) -> list[MemorySource]:
    rows = conn.execute(
        "SELECT memory_id, note_id, relation, created_at FROM memory_sources WHERE memory_id = ? ORDER BY created_at",
        (memory_id,),
    ).fetchall()
    return [MemorySource(memory_id=row["memory_id"], note_id=row["note_id"], relation=row["relation"], created_at=row["created_at"]) for row in rows]


def _load_versions(conn: sqlite3.Connection, memory_id: str) -> list[MemoryVersion]:
    rows = conn.execute(
        """
        SELECT id, memory_id, version, content, status, reason, source_note_id, created_at
        FROM memory_versions WHERE memory_id = ? ORDER BY version
        """,
        (memory_id,),
    ).fetchall()
    return [
        MemoryVersion(
            id=row["id"],
            memory_id=row["memory_id"],
            version=int(row["version"]),
            content=row["content"],
            status=row["status"],
            reason=row["reason"],
            source_note_id=row["source_note_id"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


def _add_version(
    conn: sqlite3.Connection,
    memory_id: str,
    version: int,
    content: str,
    status: str,
    *,
    reason: str | None = None,
    source_note_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO memory_versions(id, memory_id, version, content, status, reason, source_note_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (new_id("ver"), memory_id, version, content, status, reason, source_note_id, utc_now_iso()),
    )


def add_source(memory_id: str, note_id: str, relation: str, db_path: str | Path | None = None) -> bool:
    init_db(db_path)

    def _operation() -> bool:
        with _connect(db_path) as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO memory_sources(memory_id, note_id, relation, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id, note_id, relation, utc_now_iso()),
            )
            return cur.rowcount > 0

    return _run_write(_operation)


def insert_memory(
    space_id: str,
    candidate: MemoryCandidate,
    *,
    source_note_id: str,
    source_relation: str = "created_from",
    status: str = "active",
    db_path: str | Path | None = None,
) -> MemoryRecord:
    if status not in MEMORY_STATUSES:
        raise ValueError(f"invalid memory status: {status}")
    init_db(db_path)
    memory_id = new_id("mem")
    now = utc_now_iso()

    def _operation() -> None:
        with _connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO memories(
                    id, space_id, memory_type, content, normalized_content, importance, confidence,
                    status, task_status, valid_from, valid_until, created_at, updated_at,
                    last_accessed_at, access_count, current_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, 1)
                """,
                (
                    memory_id,
                    space_id,
                    candidate.memory_type,
                    candidate.content,
                    candidate.normalized_content,
                    float(candidate.importance),
                    float(candidate.confidence),
                    status,
                    candidate.task_status,
                    now,
                    None,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO memory_sources(memory_id, note_id, relation, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id, source_note_id, source_relation, now),
            )
            _add_version(conn, memory_id, 1, candidate.content, status, reason="created", source_note_id=source_note_id)

    _run_write(_operation)

    record = get_memory(memory_id, db_path=db_path)
    if record is None:
        raise RuntimeError(f"failed to read inserted memory: {memory_id}")
    return record


def get_memory(memory_id: str, db_path: str | Path | None = None) -> MemoryRecord | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            return None
        return _memory_from_row(row, sources=_load_sources(conn, memory_id), versions=_load_versions(conn, memory_id))


def list_memories(
    space_id: str,
    *,
    status: str | None = "active",
    memory_type: str | None = None,
    limit: int = 20,
    db_path: str | Path | None = None,
) -> list[MemoryRecord]:
    init_db(db_path)
    clauses = ["space_id = ?"]
    params: list[Any] = [space_id]
    if status:
        clauses.append("status = ?")
        params.append(status)
    if memory_type:
        clauses.append("memory_type = ?")
        params.append(memory_type)
    params.append(max(1, min(int(limit), 100)))
    sql = f"SELECT * FROM memories WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?"
    with _connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [_memory_from_row(row, sources=_load_sources(conn, row["id"])) for row in rows]


def update_memory(
    memory_id: str,
    *,
    content: str | None = None,
    status: str | None = None,
    task_status: str | None = None,
    valid_until: str | None = None,
    reason: str | None = None,
    source_note_id: str | None = None,
    db_path: str | Path | None = None,
) -> MemoryRecord | None:
    init_db(db_path)

    def _operation() -> bool:
        with _connect(db_path) as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if row is None:
                return False
            next_version = int(row["current_version"] or 1) + 1
            next_content = content if content is not None else str(row["content"])
            next_status = status if status is not None else str(row["status"])
            now = utc_now_iso()
            conn.execute(
                """
                UPDATE memories
                SET content = ?, normalized_content = ?, status = ?, task_status = COALESCE(?, task_status),
                    valid_until = COALESCE(?, valid_until), updated_at = ?, current_version = ?
                WHERE id = ?
                """,
                (next_content, normalize_content(next_content), next_status, task_status, valid_until, now, next_version, memory_id),
            )
            _add_version(conn, memory_id, next_version, next_content, next_status, reason=reason, source_note_id=source_note_id)
            return True

    if not _run_write(_operation):
        return None
    return get_memory(memory_id, db_path=db_path)


def mark_accessed(memory_ids: list[str], db_path: str | Path | None = None) -> None:
    if not memory_ids:
        return
    init_db(db_path)
    now = utc_now_iso()

    def _operation() -> None:
        with _connect(db_path) as conn:
            conn.executemany(
                "UPDATE memories SET last_accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
                [(now, memory_id) for memory_id in memory_ids],
            )

    _run_write(_operation)


def soft_delete_memory(memory_id: str, *, reason: str = "user_forget", db_path: str | Path | None = None) -> MemoryRecord | None:
    return update_memory(memory_id, status="deleted", reason=reason, db_path=db_path)


def correct_memory(memory_id: str, content: str, *, reason: str = "user_correct", db_path: str | Path | None = None) -> MemoryRecord | None:
    return update_memory(memory_id, content=content, status="active", reason=reason, db_path=db_path)


def purge_memory(memory_id: str, db_path: str | Path | None = None) -> bool:
    """Permanently remove one memory and its local indexes after a soft delete or explicit user request."""
    init_db(db_path)

    def _operation() -> bool:
        with _connect(db_path) as conn:
            exists = conn.execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if exists is None:
                return False
            conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (memory_id,))
            conn.execute("DELETE FROM memory_sources WHERE memory_id = ?", (memory_id,))
            conn.execute("DELETE FROM memory_versions WHERE memory_id = ?", (memory_id,))
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return True

    return _run_write(_operation)


def note_has_memory(note_id: str, db_path: str | Path | None = None) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT 1 FROM memory_sources WHERE note_id = ? LIMIT 1", (note_id,)).fetchone()
        return row is not None


def get_extraction_state(note_id: str, db_path: str | Path | None = None) -> MemoryExtractionState | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM memory_extraction_states WHERE note_id = ?", (note_id,)).fetchone()
    return _extraction_state_from_row(row) if row is not None else None


def _mark_extraction_state(
    note_id: str,
    space_id: str,
    status: str,
    *,
    candidate_count: int = 0,
    processed_count: int = 0,
    error: str | None = None,
    increment_attempt: bool = False,
    db_path: str | Path | None = None,
) -> MemoryExtractionState:
    if status not in MEMORY_EXTRACTION_STATUSES:
        raise ValueError(f"invalid memory extraction status: {status}")
    init_db(db_path)
    now = utc_now_iso()
    started_at = now if status == "processing" else None
    completed_at = now if status in {"completed", "empty", "partial", "failed"} else None
    attempt_delta = 1 if increment_attempt else 0

    def _operation() -> None:
        with _connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO memory_extraction_states(
                    note_id, space_id, status, candidate_count, processed_count, attempt_count,
                    last_error, started_at, completed_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(note_id) DO UPDATE SET
                    space_id = excluded.space_id,
                    status = excluded.status,
                    candidate_count = excluded.candidate_count,
                    processed_count = excluded.processed_count,
                    attempt_count = memory_extraction_states.attempt_count + ?,
                    last_error = excluded.last_error,
                    started_at = COALESCE(excluded.started_at, memory_extraction_states.started_at),
                    completed_at = excluded.completed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    note_id,
                    space_id,
                    status,
                    max(0, int(candidate_count)),
                    max(0, int(processed_count)),
                    attempt_delta,
                    error,
                    started_at,
                    completed_at,
                    now,
                    attempt_delta,
                ),
            )

    _run_write(_operation)
    state = get_extraction_state(note_id, db_path=db_path)
    if state is None:
        raise RuntimeError(f"failed to read extraction state: {note_id}")
    return state


def mark_extraction_processing(note_id: str, space_id: str, db_path: str | Path | None = None) -> MemoryExtractionState:
    return _mark_extraction_state(note_id, space_id, "processing", increment_attempt=True, db_path=db_path)


def mark_extraction_completed(
    note_id: str,
    space_id: str,
    *,
    candidate_count: int,
    processed_count: int,
    db_path: str | Path | None = None,
) -> MemoryExtractionState:
    return _mark_extraction_state(
        note_id,
        space_id,
        "completed",
        candidate_count=candidate_count,
        processed_count=processed_count,
        db_path=db_path,
    )


def mark_extraction_empty(note_id: str, space_id: str, db_path: str | Path | None = None) -> MemoryExtractionState:
    return _mark_extraction_state(note_id, space_id, "empty", db_path=db_path)


def mark_extraction_partial(
    note_id: str,
    space_id: str,
    *,
    candidate_count: int,
    processed_count: int,
    error: str,
    db_path: str | Path | None = None,
) -> MemoryExtractionState:
    return _mark_extraction_state(
        note_id,
        space_id,
        "partial",
        candidate_count=candidate_count,
        processed_count=processed_count,
        error=error,
        db_path=db_path,
    )


def mark_extraction_failed(note_id: str, space_id: str, *, error: str, db_path: str | Path | None = None) -> MemoryExtractionState:
    return _mark_extraction_state(note_id, space_id, "failed", error=error, db_path=db_path)


def list_retryable_extraction_states(
    space_id: str,
    *,
    limit: int = 100,
    db_path: str | Path | None = None,
) -> list[MemoryExtractionState]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM memory_extraction_states
            WHERE space_id = ? AND status IN ('pending', 'failed', 'partial')
            ORDER BY updated_at ASC
            LIMIT ?
            """,
            (space_id, max(1, min(int(limit), 500))),
        ).fetchall()
    return [_extraction_state_from_row(row) for row in rows]


def consolidation_period_key(cadence: str, day: date) -> str:
    cadence = cadence.strip().lower()
    if cadence == "daily":
        return day.isoformat()
    if cadence == "weekly":
        year, week, _weekday = day.isocalendar()
        return f"{year}-W{week:02d}"
    if cadence == "monthly":
        return f"{day.year:04d}-{day.month:02d}"
    raise ValueError(f"unknown memory consolidation cadence: {cadence}")


def reserve_consolidation_run(
    space_id: str,
    cadence: str,
    period_key: str,
    db_path: str | Path | None = None,
) -> ConsolidationRun | None:
    cadence = cadence.strip().lower()
    init_db(db_path)

    def _operation() -> str | None:
        with _connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM memory_consolidation_runs
                WHERE space_id = ? AND cadence = ? AND period_key = ?
                """,
                (space_id, cadence, period_key),
            ).fetchone()
            now = utc_now_iso()
            if row is not None:
                status = str(row["status"])
                if status == "completed":
                    return None
                if status == "running" and not _is_stale(
                    row["started_at"],
                    lease_seconds=MEMORY_CONSOLIDATION_RUN_LEASE_SECONDS,
                ):
                    return None
            run_id = new_id("run")
            conn.execute(
                """
                INSERT INTO memory_consolidation_runs(
                    id, space_id, cadence, period_key, status, started_at, completed_at, error, result_json
                )
                VALUES (?, ?, ?, ?, 'running', ?, NULL, NULL, NULL)
                ON CONFLICT(space_id, cadence, period_key) DO UPDATE SET
                    id = excluded.id,
                    status = 'running',
                    started_at = excluded.started_at,
                    completed_at = NULL,
                    error = NULL,
                    result_json = NULL
                """,
                (run_id, space_id, cadence, period_key, now),
            )
            return run_id

    run_id = _run_write(_operation)
    if run_id is None:
        return None
    return get_consolidation_run(run_id, db_path=db_path)


def get_consolidation_run(run_id: str, db_path: str | Path | None = None) -> ConsolidationRun | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM memory_consolidation_runs WHERE id = ?", (run_id,)).fetchone()
    return _consolidation_run_from_row(row) if row is not None else None


def mark_consolidation_completed(run_id: str, result: dict[str, Any], db_path: str | Path | None = None) -> None:
    init_db(db_path)
    now = utc_now_iso()
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True)

    def _operation() -> None:
        with _connect(db_path) as conn:
            conn.execute(
                """
                UPDATE memory_consolidation_runs
                SET status = 'completed', completed_at = ?, error = NULL, result_json = ?
                WHERE id = ?
                """,
                (now, payload, run_id),
            )

    _run_write(_operation)


def mark_consolidation_failed(run_id: str, error: str, db_path: str | Path | None = None) -> None:
    init_db(db_path)
    now = utc_now_iso()

    def _operation() -> None:
        with _connect(db_path) as conn:
            conn.execute(
                """
                UPDATE memory_consolidation_runs
                SET status = 'failed', completed_at = ?, error = ?
                WHERE id = ?
                """,
                (now, error, run_id),
            )

    _run_write(_operation)


def search_memories(
    space_id: str,
    query: str,
    *,
    memory_type: str | None = None,
    include_inactive: bool = False,
    min_score: float = MEMORY_QUERY_MIN_SCORE,
    limit: int = 10,
    mark_access: bool = True,
    db_path: str | Path | None = None,
) -> list[tuple[MemoryRecord, float]]:
    from memory.retriever import score_memory

    candidates = list_memories(
        space_id,
        status=None if include_inactive else "active",
        memory_type=memory_type,
        limit=100,
        db_path=db_path,
    )
    scored = [(memory, score_memory(query, memory)) for memory in candidates]
    scored = [(memory, score) for memory, score in scored if score >= min_score]
    scored.sort(key=lambda item: item[1], reverse=True)
    limited = scored[: max(1, min(int(limit), 50))]
    if mark_access:
        mark_accessed([memory.id for memory, _score in limited], db_path=db_path)
    return limited


def stats(space_id: str, db_path: str | Path | None = None) -> dict[str, Any]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT memory_type, status, COUNT(*) AS count FROM memories WHERE space_id = ? GROUP BY memory_type, status",
            (space_id,),
        ).fetchall()
        extraction_rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM memory_extraction_states WHERE space_id = ? GROUP BY status",
            (space_id,),
        ).fetchall()
        retryable_row = conn.execute(
            """
            SELECT COUNT(*) AS count FROM memory_extraction_states
            WHERE space_id = ? AND status IN ('pending', 'failed', 'partial')
            """,
            (space_id,),
        ).fetchone()
        run_rows = conn.execute(
            """
            SELECT id, cadence, period_key, status, started_at, completed_at, error
            FROM memory_consolidation_runs
            WHERE space_id = ?
            ORDER BY started_at DESC
            LIMIT 5
            """,
            (space_id,),
        ).fetchall()
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    total = 0
    for row in rows:
        count = int(row["count"])
        total += count
        by_type[str(row["memory_type"])] = by_type.get(str(row["memory_type"]), 0) + count
        by_status[str(row["status"])] = by_status.get(str(row["status"]), 0) + count
    extraction_by_status = {str(row["status"]): int(row["count"]) for row in extraction_rows}
    consolidation_last_runs = [
        {
            "id": row["id"],
            "cadence": row["cadence"],
            "period_key": row["period_key"],
            "status": row["status"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "error": row["error"],
        }
        for row in run_rows
    ]
    return {
        "total": total,
        "by_type": by_type,
        "by_status": by_status,
        "extraction_by_status": extraction_by_status,
        "retryable_extraction_count": int(retryable_row["count"] if retryable_row else 0),
        "consolidation_last_runs": consolidation_last_runs,
    }


def schema_tables(db_path: str | Path | None = None) -> set[str]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}
