"""SQLite repository for versioned, auditable long-term memory."""

from __future__ import annotations

import json
import sqlite3
import threading
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
    DECISION_ACTIONS,
    MEMORY_EXTRACTION_STATUSES,
    MEMORY_RELATION_TYPES,
    MEMORY_STATUSES,
    SOURCE_RELATIONS,
    ConsolidationRun,
    MemoryCandidate,
    MemoryDecision,
    MemoryExtractionState,
    MemoryRecord,
    MemoryRelation,
    MemorySource,
    MemoryVersion,
    memory_key_for,
    new_id,
    normalize_content,
    utc_now_iso,
)

DB_PATH = Path("data/memory/memory.db")
T = TypeVar("T")
_SCHEMA_LOCK = threading.RLock()


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


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Apply an additive SQLite migration for databases created by older releases."""
    existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db(db_path: str | Path | None = None) -> None:
    with _SCHEMA_LOCK:
        _init_db(db_path)


def _init_db(db_path: str | Path | None = None) -> None:
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
                subject TEXT,
                predicate TEXT,
                object_value TEXT,
                memory_key TEXT,
                polarity TEXT,
                scope_json TEXT NOT NULL DEFAULT '{}',
                valid_from TEXT,
                valid_until TEXT,
                last_confirmed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_accessed_at TEXT,
                access_count INTEGER DEFAULT 0,
                current_version INTEGER DEFAULT 1
            );

            CREATE INDEX IF NOT EXISTS idx_memories_space_status
            ON memories(space_id, status, memory_type);

            CREATE TABLE IF NOT EXISTS memory_candidates (
                candidate_id TEXT PRIMARY KEY,
                space_id TEXT NOT NULL,
                note_id TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                content TEXT NOT NULL,
                normalized_content TEXT NOT NULL,
                memory_key TEXT NOT NULL,
                subject TEXT,
                predicate TEXT,
                object_value TEXT,
                task_status TEXT,
                polarity TEXT,
                scope_json TEXT NOT NULL DEFAULT '{}',
                entities_json TEXT NOT NULL DEFAULT '[]',
                valid_from TEXT,
                valid_until TEXT,
                confidence REAL NOT NULL,
                importance REAL NOT NULL,
                evidence_span TEXT,
                should_store INTEGER NOT NULL DEFAULT 1,
                extractor_type TEXT NOT NULL DEFAULT 'rules',
                extractor_version TEXT NOT NULL DEFAULT 'memory-extractor-v1',
                model TEXT,
                prompt_hash TEXT,
                status TEXT NOT NULL DEFAULT 'extracted',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                decision_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                applied_at TEXT,
                UNIQUE(note_id, candidate_id)
            );

            CREATE INDEX IF NOT EXISTS idx_memory_candidates_space_status
            ON memory_candidates(space_id, status, updated_at);

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
                task_status TEXT,
                confidence REAL,
                importance REAL,
                valid_from TEXT,
                valid_until TEXT,
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

            CREATE TABLE IF NOT EXISTS memory_decisions (
                id TEXT PRIMARY KEY,
                space_id TEXT NOT NULL,
                note_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                target_memory_ids_json TEXT NOT NULL,
                confidence REAL NOT NULL,
                reason TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                recommended_action TEXT NOT NULL,
                status TEXT NOT NULL,
                result_memory_ids_json TEXT,
                error TEXT,
                policy_version TEXT NOT NULL DEFAULT 'memory-policy-v1',
                adjudicator_version TEXT NOT NULL DEFAULT 'memory-adjudicator-v1',
                model TEXT,
                prompt_hash TEXT,
                input_hash TEXT,
                target_snapshot_version INTEGER,
                retry_of_decision_id TEXT,
                created_at TEXT NOT NULL,
                applied_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_memory_decisions_note
            ON memory_decisions(space_id, note_id, created_at);

            CREATE TABLE IF NOT EXISTS memory_relations (
                id TEXT PRIMARY KEY,
                space_id TEXT NOT NULL,
                source_memory_id TEXT NOT NULL,
                target_memory_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                decision_id TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(source_memory_id, target_memory_id, relation, decision_id)
            );

            CREATE INDEX IF NOT EXISTS idx_memory_relations_source
            ON memory_relations(space_id, source_memory_id, relation);

            CREATE INDEX IF NOT EXISTS idx_memory_relations_target
            ON memory_relations(space_id, target_memory_id, relation);

            CREATE TABLE IF NOT EXISTS memory_traces (
                trace_id TEXT PRIMARY KEY,
                space_id TEXT NOT NULL,
                note_id TEXT,
                trace_type TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_memory_traces_space
            ON memory_traces(space_id, finished_at);
            """
        )
        _ensure_column(conn, "memories", "subject", "TEXT")
        _ensure_column(conn, "memories", "predicate", "TEXT")
        _ensure_column(conn, "memories", "object_value", "TEXT")
        _ensure_column(conn, "memories", "memory_key", "TEXT")
        _ensure_column(conn, "memories", "polarity", "TEXT")
        _ensure_column(conn, "memories", "scope_json", "TEXT NOT NULL DEFAULT '{}'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_space_key_status ON memories(space_id, memory_key, status)")
        _ensure_column(conn, "memories", "last_confirmed_at", "TEXT")
        _ensure_column(conn, "memory_versions", "task_status", "TEXT")
        _ensure_column(conn, "memory_versions", "confidence", "REAL")
        _ensure_column(conn, "memory_versions", "importance", "REAL")
        _ensure_column(conn, "memory_versions", "valid_from", "TEXT")
        _ensure_column(conn, "memory_versions", "valid_until", "TEXT")
        _ensure_column(conn, "memory_decisions", "policy_version", "TEXT NOT NULL DEFAULT 'memory-policy-v1'")
        _ensure_column(conn, "memory_decisions", "adjudicator_version", "TEXT NOT NULL DEFAULT 'memory-adjudicator-v1'")
        _ensure_column(conn, "memory_decisions", "model", "TEXT")
        _ensure_column(conn, "memory_decisions", "prompt_hash", "TEXT")
        _ensure_column(conn, "memory_decisions", "input_hash", "TEXT")
        _ensure_column(conn, "memory_decisions", "target_snapshot_version", "INTEGER")
        _ensure_column(conn, "memory_decisions", "retry_of_decision_id", "TEXT")
        _ensure_column(conn, "memory_candidates", "task_status", "TEXT")


def _memory_from_row(row: sqlite3.Row, *, sources: list[MemorySource] | None = None, versions: list[MemoryVersion] | None = None) -> MemoryRecord:
    try:
        scope = json.loads(row["scope_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        scope = {}
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
        subject=row["subject"],
        predicate=row["predicate"],
        object_value=row["object_value"],
        last_confirmed_at=row["last_confirmed_at"],
        memory_key=row["memory_key"] or None,
        polarity=row["polarity"] or None,
        scope=scope if isinstance(scope, dict) else {},
        sources=sources or [],
        versions=versions or [],
    )


def _candidate_from_row(row: sqlite3.Row) -> MemoryCandidate:
    try:
        scope = json.loads(row["scope_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        scope = {}
    try:
        entities = json.loads(row["entities_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        entities = []
    return MemoryCandidate(
        memory_type=str(row["memory_type"]),
        content=str(row["content"]),
        importance=float(row["importance"]),
        confidence=float(row["confidence"]),
        entities=list(entities) if isinstance(entities, list) else [],
        should_store=bool(row["should_store"]),
        task_status=row["task_status"],
        candidate_id=str(row["candidate_id"]),
        note_id=str(row["note_id"]),
        space_id=str(row["space_id"]),
        subject=row["subject"],
        predicate=row["predicate"],
        object_value=row["object_value"],
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
        evidence_span=row["evidence_span"],
        memory_key=row["memory_key"],
        polarity=row["polarity"],
        scope=scope if isinstance(scope, dict) else {},
        extractor_type=str(row["extractor_type"]),
        extractor_version=str(row["extractor_version"]),
        model=row["model"],
        prompt_hash=row["prompt_hash"],
    )


def save_memory_candidate(
    candidate: MemoryCandidate,
    *,
    space_id: str,
    status: str = "extracted",
    error: str | None = None,
    decision_id: str | None = None,
    db_path: str | Path | None = None,
) -> MemoryCandidate:
    """Persist one candidate without resetting a terminal retry state."""
    init_db(db_path)
    now = utc_now_iso()

    def _operation() -> None:
        with _connect(db_path) as conn:
            existing = conn.execute(
                "SELECT status, attempt_count FROM memory_candidates WHERE candidate_id = ?",
                (candidate.candidate_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO memory_candidates(
                        candidate_id, space_id, note_id, memory_type, content, normalized_content,
                        memory_key, subject, predicate, object_value, task_status, polarity,
                        scope_json, entities_json, valid_from, valid_until, confidence, importance,
                        evidence_span, should_store, extractor_type, extractor_version, model,
                        prompt_hash, status, attempt_count, last_error, decision_id, created_at, updated_at, applied_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        candidate.candidate_id,
                        space_id,
                        candidate.note_id or "",
                        candidate.memory_type,
                        candidate.content,
                        candidate.normalized_content,
                        candidate.effective_memory_key,
                        candidate.subject,
                        candidate.predicate,
                        candidate.object_value,
                        candidate.task_status,
                        candidate.polarity,
                        json.dumps(candidate.scope, ensure_ascii=False),
                        json.dumps(candidate.entities, ensure_ascii=False),
                        candidate.valid_from,
                        candidate.valid_until,
                        float(candidate.confidence),
                        float(candidate.importance),
                        candidate.evidence_span,
                        int(candidate.should_store),
                        candidate.extractor_type,
                        candidate.extractor_version,
                        candidate.model,
                        candidate.prompt_hash,
                        status,
                        0,
                        error,
                        decision_id,
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE memory_candidates
                    SET updated_at = ?, last_error = COALESCE(?, last_error), decision_id = COALESCE(?, decision_id)
                    WHERE candidate_id = ?
                    """,
                    (now, error, decision_id, candidate.candidate_id),
                )

    _run_write(_operation)
    stored = get_memory_candidate(candidate.candidate_id, db_path=db_path)
    if stored is None:
        raise RuntimeError(f"failed to persist memory candidate: {candidate.candidate_id}")
    return stored


def get_memory_candidate(candidate_id: str, db_path: str | Path | None = None) -> MemoryCandidate | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM memory_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
    return _candidate_from_row(row) if row is not None else None


def get_memory_candidate_status(candidate_id: str, db_path: str | Path | None = None) -> str | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT status FROM memory_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
    return str(row["status"]) if row is not None else None


def mark_memory_candidate(
    candidate_id: str,
    status: str,
    *,
    error: str | None = None,
    decision_id: str | None = None,
    db_path: str | Path | None = None,
) -> bool:
    allowed = {"extracted", "validated", "adjudicated", "applied", "pending_review", "discarded", "failed", "processing"}
    if status not in allowed:
        raise ValueError(f"invalid memory candidate status: {status}")
    init_db(db_path)
    now = utc_now_iso()

    def _operation() -> bool:
        with _connect(db_path) as conn:
            result = conn.execute(
                """
                UPDATE memory_candidates
                SET status = ?, attempt_count = attempt_count + ?, last_error = ?, decision_id = COALESCE(?, decision_id),
                    updated_at = ?, applied_at = CASE WHEN ? IN ('applied', 'pending_review', 'discarded') THEN ? ELSE applied_at END
                WHERE candidate_id = ?
                """,
                (status, int(status == "processing"), error, decision_id, now, status, now, candidate_id),
            )
            return bool(result.rowcount)

    return _run_write(_operation)


def list_retryable_memory_candidates(space_id: str, *, limit: int = 100, db_path: str | Path | None = None) -> list[MemoryCandidate]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM memory_candidates
            WHERE space_id = ? AND status IN ('extracted', 'validated', 'failed', 'processing')
            ORDER BY updated_at LIMIT ?
            """,
            (space_id, max(1, min(int(limit), 500))),
        ).fetchall()
    return [_candidate_from_row(row) for row in rows]


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
        SELECT id, memory_id, version, content, status, task_status, confidence, importance,
               valid_from, valid_until, reason, source_note_id, created_at
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
            task_status=row["task_status"],
            confidence=float(row["confidence"]) if row["confidence"] is not None else None,
            importance=float(row["importance"]) if row["importance"] is not None else None,
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
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
    task_status: str | None = None,
    confidence: float | None = None,
    importance: float | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    reason: str | None = None,
    source_note_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO memory_versions(
            id, memory_id, version, content, status, task_status, confidence, importance,
            valid_from, valid_until, reason, source_note_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id("ver"),
            memory_id,
            version,
            content,
            status,
            task_status,
            confidence,
            importance,
            valid_from,
            valid_until,
            reason,
            source_note_id,
            utc_now_iso(),
        ),
    )


def _add_source_row(conn: sqlite3.Connection, memory_id: str, note_id: str, relation: str, *, now: str | None = None) -> bool:
    if relation not in SOURCE_RELATIONS:
        raise ValueError(f"invalid source relation: {relation}")
    existing = conn.execute(
        "SELECT 1 FROM memory_sources WHERE memory_id = ? AND note_id = ? LIMIT 1",
        (memory_id, note_id),
    ).fetchone()
    if existing is not None:
        return False
    conn.execute(
        """
        INSERT INTO memory_sources(memory_id, note_id, relation, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (memory_id, note_id, relation, now or utc_now_iso()),
    )
    return True


def _insert_memory_row(
    conn: sqlite3.Connection,
    space_id: str,
    candidate: MemoryCandidate,
    *,
    source_note_id: str,
    source_relation: str = "created_from",
    status: str = "active",
    memory_id: str | None = None,
    now: str | None = None,
) -> str:
    if status not in MEMORY_STATUSES:
        raise ValueError(f"invalid memory status: {status}")
    created_at = now or utc_now_iso()
    record_id = memory_id or new_id("mem")
    valid_from = candidate.valid_from or created_at
    conn.execute(
        """
        INSERT INTO memories(
            id, space_id, memory_type, content, normalized_content, importance, confidence,
            status, task_status, subject, predicate, object_value, memory_key, polarity, scope_json, valid_from, valid_until,
            last_confirmed_at, created_at, updated_at, last_accessed_at, access_count, current_version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_id,
            space_id,
            candidate.memory_type,
            candidate.content,
            candidate.normalized_content,
            float(candidate.importance),
            float(candidate.confidence),
            status,
            candidate.task_status,
            candidate.subject,
            candidate.predicate,
            candidate.object_value,
            candidate.effective_memory_key,
            candidate.polarity,
            json.dumps(candidate.scope, ensure_ascii=False),
            valid_from,
            candidate.valid_until,
            None,
            created_at,
            created_at,
            None,
            0,
            1,
        ),
    )
    _add_source_row(conn, record_id, source_note_id, source_relation, now=created_at)
    _add_version(
        conn,
        record_id,
        1,
        candidate.content,
        status,
        task_status=candidate.task_status,
        confidence=float(candidate.confidence),
        importance=float(candidate.importance),
        valid_from=valid_from,
        valid_until=candidate.valid_until,
        reason="created",
        source_note_id=source_note_id,
    )
    return record_id


def _add_relation_row(
    conn: sqlite3.Connection,
    *,
    space_id: str,
    source_memory_id: str,
    target_memory_id: str,
    relation: str,
    decision_id: str | None,
    now: str,
) -> None:
    if relation not in MEMORY_RELATION_TYPES:
        raise ValueError(f"invalid memory relation: {relation}")
    existing = conn.execute(
        """
        SELECT 1 FROM memory_relations
        WHERE source_memory_id = ? AND target_memory_id = ? AND relation = ?
        LIMIT 1
        """,
        (source_memory_id, target_memory_id, relation),
    ).fetchone()
    if existing is not None:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO memory_relations(
            id, space_id, source_memory_id, target_memory_id, relation, decision_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (new_id("rel"), space_id, source_memory_id, target_memory_id, relation, decision_id, now),
    )


def _insert_decision_row(
    conn: sqlite3.Connection,
    *,
    space_id: str,
    note_id: str,
    decision: MemoryDecision,
    status: str,
    result_memory_ids: list[str] | None = None,
    error: str | None = None,
    now: str | None = None,
) -> None:
    if decision.recommended_action not in DECISION_ACTIONS:
        raise ValueError(f"invalid decision action: {decision.recommended_action}")
    created_at = now or utc_now_iso()
    conn.execute(
        """
        INSERT OR REPLACE INTO memory_decisions(
            id, space_id, note_id, candidate_id, relation, target_memory_ids_json,
            confidence, reason, evidence_json, recommended_action, status,
            result_memory_ids_json, error, policy_version, adjudicator_version, model,
            prompt_hash, input_hash, target_snapshot_version, retry_of_decision_id, created_at, applied_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision.decision_id,
            space_id,
            note_id,
            decision.candidate_id,
            decision.relation,
            json.dumps(decision.target_memory_ids, ensure_ascii=False),
            float(decision.confidence),
            decision.reason,
            json.dumps(decision.evidence, ensure_ascii=False),
            decision.recommended_action,
            status,
            json.dumps(result_memory_ids or [], ensure_ascii=False),
            error,
            decision.policy_version,
            decision.adjudicator_version,
            decision.model,
            decision.prompt_hash,
            decision.input_hash,
            decision.target_snapshot_version,
            decision.retry_of_decision_id,
            created_at,
            created_at if status == "applied" else None,
        ),
    )


def add_source(memory_id: str, note_id: str, relation: str, db_path: str | Path | None = None) -> bool:
    init_db(db_path)

    def _operation() -> bool:
        with _connect(db_path) as conn:
            return _add_source_row(conn, memory_id, note_id, relation)

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

    def _operation() -> None:
        with _connect(db_path) as conn:
            _insert_memory_row(
                conn,
                space_id,
                candidate,
                source_note_id=source_note_id,
                source_relation=source_relation,
                status=status,
                memory_id=memory_id,
            )

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
    memory_key: str | None = None,
    include_expired: bool = False,
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
    if memory_key:
        clauses.append("memory_key = ?")
        params.append(memory_key)
    if status == "active" and not include_expired:
        clauses.append("(valid_until IS NULL OR valid_until > ?)")
        params.append(utc_now_iso())
    params.append(max(1, min(int(limit), 100)))
    sql = f"SELECT * FROM memories WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?"
    with _connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [_memory_from_row(row, sources=_load_sources(conn, row["id"])) for row in rows]


def list_adjudication_candidates(
    space_id: str,
    *,
    memory_type: str,
    memory_key: str,
    limit: int = 200,
    db_path: str | Path | None = None,
) -> list[MemoryRecord]:
    memories = list_memories(
        space_id,
        status="active",
        memory_type=memory_type,
        limit=min(int(limit), 100),
        db_path=db_path,
    )
    memories.sort(key=lambda memory: (memory.effective_memory_key == memory_key, memory.updated_at), reverse=True)
    return memories


def hybrid_adjudication_candidates(
    space_id: str,
    candidate: MemoryCandidate,
    *,
    query_embedding: list[float] | None = None,
    limit: int = 20,
    db_path: str | Path | None = None,
) -> list[MemoryRecord]:
    del query_embedding
    init_db(db_path)
    now = utc_now_iso()
    with _connect(db_path) as conn:
        exact_rows = conn.execute(
            """
            SELECT * FROM memories
            WHERE space_id = ? AND status = 'active' AND memory_type = ?
              AND memory_key = ? AND (valid_until IS NULL OR valid_until > ?)
            ORDER BY updated_at DESC
            LIMIT 20
            """,
            (space_id, candidate.memory_type, candidate.effective_memory_key, now),
        ).fetchall()
        structured_params: list[Any] = [space_id, candidate.memory_type, now]
        structured_clauses = [
            "space_id = ?",
            "status = 'active'",
            "memory_type = ?",
            "(valid_until IS NULL OR valid_until > ?)",
        ]
        optional = []
        if candidate.subject:
            optional.append("lower(subject) = ?")
            structured_params.append(candidate.subject.casefold())
        if candidate.predicate:
            optional.append("lower(predicate) = ?")
            structured_params.append(candidate.predicate.casefold())
        if candidate.object_value:
            optional.append("object_value LIKE ?")
            structured_params.append(f"%{candidate.object_value[:120]}%")
        for entity in candidate.entities[:5]:
            optional.append("content LIKE ?")
            structured_params.append(f"%{str(entity)[:120]}%")
        structured_rows = []
        if optional:
            structured_sql = (
                f"SELECT * FROM memories WHERE {' AND '.join(structured_clauses)} "
                f"AND ({' OR '.join(optional)}) ORDER BY updated_at DESC LIMIT ?"
            )
            structured_params.append(max(30, min(int(limit) * 3, 200)))
            structured_rows = conn.execute(structured_sql, structured_params).fetchall()
        rows_by_id = {row["id"]: row for row in [*exact_rows, *structured_rows]}
        memories = [_memory_from_row(row) for row in rows_by_id.values()]

    def _matches(memory: MemoryRecord) -> bool:
        if memory.effective_memory_key == candidate.effective_memory_key:
            return True
        if candidate.subject and memory.subject and normalize_content(candidate.subject) == normalize_content(memory.subject):
            return True
        if candidate.predicate and memory.predicate and normalize_content(candidate.predicate) == normalize_content(memory.predicate):
            return True
        if candidate.object_value and memory.object_value and normalize_content(candidate.object_value) in normalize_content(memory.object_value):
            return True
        return any(entity and entity.casefold() in memory.content.casefold() for entity in candidate.entities)

    filtered = [memory for memory in memories if _matches(memory)]
    return (filtered or memories)[: max(1, min(int(limit), 50))]


def expire_due_memories(
    space_id: str | None = None,
    *,
    limit: int = 500,
    db_path: str | Path | None = None,
) -> int:
    init_db(db_path)
    now = utc_now_iso()

    def _operation() -> int:
        with _connect(db_path) as conn:
            clauses = ["status = 'active'", "valid_until IS NOT NULL", "valid_until <= ?"]
            params: list[Any] = [now]
            if space_id:
                clauses.append("space_id = ?")
                params.append(space_id)
            params.append(max(1, min(int(limit), 1000)))
            rows = conn.execute(
                f"SELECT * FROM memories WHERE {' AND '.join(clauses)} ORDER BY valid_until LIMIT ?",
                params,
            ).fetchall()
            for row in rows:
                _versioned_update_row(
                    conn,
                    row,
                    status="expired",
                    reason="valid_until_reached",
                    source_note_id=None,
                    now=now,
                )
            return len(rows)

    return _run_write(_operation)


def update_memory(
    memory_id: str,
    *,
    content: str | None = None,
    status: str | None = None,
    task_status: str | None = None,
    valid_until: str | None = None,
    confidence: float | None = None,
    importance: float | None = None,
    last_confirmed_at: str | None = None,
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
            if next_status not in MEMORY_STATUSES:
                raise ValueError(f"invalid memory status: {next_status}")
            next_task_status = task_status if task_status is not None else row["task_status"]
            next_valid_until = valid_until if valid_until is not None else row["valid_until"]
            next_confidence = float(confidence) if confidence is not None else float(row["confidence"])
            next_importance = float(importance) if importance is not None else float(row["importance"])
            next_confirmed = last_confirmed_at if last_confirmed_at is not None else row["last_confirmed_at"]
            next_memory_key = memory_key_for(
                str(row["memory_type"]),
                subject=row["subject"],
                predicate=row["predicate"],
                object_value=row["object_value"],
                content=next_content,
            )
            next_polarity = row["polarity"]
            now = utc_now_iso()
            conn.execute(
                """
                UPDATE memories
                SET content = ?, normalized_content = ?, status = ?, task_status = ?,
                    memory_key = ?, polarity = ?, valid_until = ?, confidence = ?, importance = ?, last_confirmed_at = ?,
                    updated_at = ?, current_version = ?
                WHERE id = ?
                """,
                (
                    next_content,
                    normalize_content(next_content),
                    next_status,
                    next_task_status,
                    next_memory_key,
                    next_polarity,
                    next_valid_until,
                    next_confidence,
                    next_importance,
                    next_confirmed,
                    now,
                    next_version,
                    memory_id,
                ),
            )
            _add_version(
                conn,
                memory_id,
                next_version,
                next_content,
                next_status,
                task_status=next_task_status,
                confidence=next_confidence,
                importance=next_importance,
                valid_from=row["valid_from"],
                valid_until=next_valid_until,
                reason=reason,
                source_note_id=source_note_id,
            )
            return True

    if not _run_write(_operation):
        return None
    return get_memory(memory_id, db_path=db_path)


def _versioned_update_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    content: str | None = None,
    status: str | None = None,
    task_status: str | None = None,
    valid_until: str | None = None,
    confidence: float | None = None,
    importance: float | None = None,
    last_confirmed_at: str | None = None,
    reason: str,
    source_note_id: str | None,
    now: str,
) -> None:
    next_version = int(row["current_version"] or 1) + 1
    next_content = content if content is not None else str(row["content"])
    next_status = status if status is not None else str(row["status"])
    next_task_status = task_status if task_status is not None else row["task_status"]
    next_valid_until = valid_until if valid_until is not None else row["valid_until"]
    next_confidence = float(confidence) if confidence is not None else float(row["confidence"])
    next_importance = float(importance) if importance is not None else float(row["importance"])
    next_confirmed = last_confirmed_at if last_confirmed_at is not None else row["last_confirmed_at"]
    conn.execute(
        """
        UPDATE memories
        SET content = ?, normalized_content = ?, status = ?, task_status = ?, valid_until = ?,
            confidence = ?, importance = ?, last_confirmed_at = ?, updated_at = ?, current_version = ?
        WHERE id = ?
        """,
        (
            next_content,
            normalize_content(next_content),
            next_status,
            next_task_status,
            next_valid_until,
            next_confidence,
            next_importance,
            next_confirmed,
            now,
            next_version,
            row["id"],
        ),
    )
    _add_version(
        conn,
        str(row["id"]),
        next_version,
        next_content,
        next_status,
        task_status=next_task_status,
        confidence=next_confidence,
        importance=next_importance,
        valid_from=row["valid_from"],
        valid_until=next_valid_until,
        reason=reason,
        source_note_id=source_note_id,
    )


def apply_memory_decision(
    space_id: str,
    note_id: str,
    candidate: MemoryCandidate,
    decision: MemoryDecision,
    *,
    merged_content: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Apply one adjudicated action and its audit records in a single transaction."""
    init_db(db_path)

    def _operation() -> dict[str, Any]:
        with _connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utc_now_iso()
            action = decision.recommended_action
            result_memory_ids: list[str] = []
            result: dict[str, Any] = {
                "action": action,
                "relation": decision.relation,
                "decision_id": decision.decision_id,
                "candidate_id": candidate.candidate_id,
                "confidence": decision.confidence,
            }
            target_id = decision.target_memory_ids[0] if decision.target_memory_ids else None
            target_row = None
            if target_id:
                target_row = conn.execute("SELECT * FROM memories WHERE id = ?", (target_id,)).fetchone()
                if target_row is None:
                    raise ValueError(f"decision target memory not found: {target_id}")

            if action == "discard":
                pass
            elif action == "insert":
                memory_id = _insert_memory_row(conn, space_id, candidate, source_note_id=note_id, now=now)
                result_memory_ids.append(memory_id)
                result["memory_id"] = memory_id
            elif action == "pending_review":
                memory_id = _insert_memory_row(
                    conn,
                    space_id,
                    candidate,
                    source_note_id=note_id,
                    status="pending_review",
                    now=now,
                )
                result_memory_ids.append(memory_id)
                result["memory_id"] = memory_id
                if target_id:
                    result["target_memory_id"] = target_id
            elif action == "add_source" and target_row is not None:
                source_added = _add_source_row(conn, target_id, note_id, "supported_by", now=now)
                if source_added:
                    old_confidence = float(target_row["confidence"])
                    strengthened = min(0.99, max(old_confidence, old_confidence + (candidate.confidence - old_confidence) * 0.25 + 0.02))
                    conn.execute(
                        """
                        UPDATE memories
                        SET confidence = ?, last_confirmed_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (strengthened, now, now, target_id),
                    )
                result_memory_ids.append(target_id)
                result.update({"memory_id": target_id, "source_added": source_added})
            elif action == "merge" and target_row is not None:
                source_added = _add_source_row(conn, target_id, note_id, "supported_by", now=now)
                if source_added:
                    _versioned_update_row(
                        conn,
                        target_row,
                        content=merged_content or candidate.content,
                        confidence=min(0.99, max(float(target_row["confidence"]), candidate.confidence)),
                        importance=max(float(target_row["importance"]), candidate.importance),
                        last_confirmed_at=now,
                        reason=decision.reason,
                        source_note_id=note_id,
                        now=now,
                    )
                result_memory_ids.append(target_id)
                result.update({"memory_id": target_id, "source_added": source_added})
            elif action == "update_task" and target_row is not None:
                source_added = _add_source_row(conn, target_id, note_id, "updated_by", now=now)
                if source_added:
                    _versioned_update_row(
                        conn,
                        target_row,
                        content=candidate.content,
                        task_status=candidate.task_status,
                        confidence=min(0.99, max(float(target_row["confidence"]), candidate.confidence)),
                        last_confirmed_at=now,
                        reason=decision.reason,
                        source_note_id=note_id,
                        now=now,
                    )
                result_memory_ids.append(target_id)
                result.update({"memory_id": target_id, "task_status": candidate.task_status, "source_added": source_added})
            elif action == "supersede" and target_row is not None:
                _add_source_row(conn, target_id, note_id, "contradicted_by", now=now)
                _versioned_update_row(
                    conn,
                    target_row,
                    status="superseded",
                    valid_until=now,
                    reason=decision.reason,
                    source_note_id=note_id,
                    now=now,
                )
                memory_id = _insert_memory_row(conn, space_id, candidate, source_note_id=note_id, now=now)
                _add_relation_row(
                    conn,
                    space_id=space_id,
                    source_memory_id=memory_id,
                    target_memory_id=target_id,
                    relation="supersedes",
                    decision_id=decision.decision_id,
                    now=now,
                )
                _add_relation_row(
                    conn,
                    space_id=space_id,
                    source_memory_id=target_id,
                    target_memory_id=memory_id,
                    relation="superseded_by",
                    decision_id=decision.decision_id,
                    now=now,
                )
                result_memory_ids.extend([memory_id, target_id])
                result.update({"memory_id": memory_id, "target_memory_id": target_id})
            elif action == "conflict" and target_row is not None:
                _add_source_row(conn, target_id, note_id, "contradicted_by", now=now)
                _versioned_update_row(
                    conn,
                    target_row,
                    status="conflicted",
                    reason=decision.reason,
                    source_note_id=note_id,
                    now=now,
                )
                memory_id = _insert_memory_row(
                    conn,
                    space_id,
                    candidate,
                    source_note_id=note_id,
                    status="conflicted",
                    now=now,
                )
                _add_relation_row(
                    conn,
                    space_id=space_id,
                    source_memory_id=memory_id,
                    target_memory_id=target_id,
                    relation="conflicts_with",
                    decision_id=decision.decision_id,
                    now=now,
                )
                _add_relation_row(
                    conn,
                    space_id=space_id,
                    source_memory_id=target_id,
                    target_memory_id=memory_id,
                    relation="conflicts_with",
                    decision_id=decision.decision_id,
                    now=now,
                )
                result_memory_ids.extend([memory_id, target_id])
                result.update({"memory_id": memory_id, "target_memory_id": target_id})
            else:
                raise ValueError(f"decision action cannot be applied: {action}")

            _insert_decision_row(
                conn,
                space_id=space_id,
                note_id=note_id,
                decision=decision,
                status="pending_review" if action == "pending_review" else "applied",
                result_memory_ids=result_memory_ids,
                now=now,
            )
            return result

    try:
        return _run_write(_operation)
    except Exception as exc:
        error_type = type(exc).__name__

        def _record_failure() -> None:
            with _connect(db_path) as conn:
                _insert_decision_row(
                    conn,
                    space_id=space_id,
                    note_id=note_id,
                    decision=decision,
                    status="failed",
                    error=error_type,
                )

        try:
            _run_write(_record_failure)
        except Exception:
            pass
        raise


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


def flush_access_counts(*, limit: int = 1000, db_path: str | Path | None = None) -> int:
    del limit, db_path
    return 0


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
            conn.execute("DELETE FROM memory_relations WHERE source_memory_id = ? OR target_memory_id = ?", (memory_id, memory_id))
            conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (memory_id,))
            conn.execute("DELETE FROM memory_sources WHERE memory_id = ?", (memory_id,))
            conn.execute("DELETE FROM memory_versions WHERE memory_id = ?", (memory_id,))
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return True

    return _run_write(_operation)


def approve_pending_memory(memory_id: str, db_path: str | Path | None = None) -> MemoryRecord | None:
    """Approve a pending candidate by applying its original relation to the target."""
    init_db(db_path)

    def _operation() -> str | None:
        with _connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            pending = conn.execute("SELECT * FROM memories WHERE id = ? AND status = 'pending_review'", (memory_id,)).fetchone()
            if pending is None:
                return None
            decision = conn.execute(
                """
                SELECT * FROM memory_decisions
                WHERE status = 'pending_review' AND result_memory_ids_json LIKE ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (f'%"{memory_id}"%',),
            ).fetchone()
            if decision is None:
                return None

            now = utc_now_iso()
            relation = str(decision["relation"])
            target_ids = json.loads(decision["target_memory_ids_json"] or "[]")
            target_id = str(target_ids[0]) if target_ids else None
            target = conn.execute("SELECT * FROM memories WHERE id = ?", (target_id,)).fetchone() if target_id else None
            if target_id and target is None:
                raise ValueError(f"pending review target memory not found: {target_id}")
            source_rows = conn.execute("SELECT note_id FROM memory_sources WHERE memory_id = ?", (memory_id,)).fetchall()
            source_note_ids = [str(row["note_id"]) for row in source_rows]
            source_note_id = source_note_ids[0] if source_note_ids else str(decision["note_id"])
            result_ids: list[str]
            result_id = memory_id

            if relation == "new":
                _versioned_update_row(
                    conn,
                    pending,
                    status="active",
                    last_confirmed_at=now,
                    reason="user_approved_pending_memory",
                    source_note_id=source_note_id,
                    now=now,
                )
                result_ids = [memory_id]
            elif relation == "merge" and target is not None:
                from memory.policies import merge_content

                for note_id in source_note_ids:
                    _add_source_row(conn, target_id, note_id, "supported_by", now=now)
                _versioned_update_row(
                    conn,
                    target,
                    content=merge_content(str(pending["memory_type"]), str(target["content"]), str(pending["content"])),
                    confidence=min(0.99, max(float(target["confidence"]), float(pending["confidence"]))),
                    importance=max(float(target["importance"]), float(pending["importance"])),
                    last_confirmed_at=now,
                    reason="user_approved_merge",
                    source_note_id=source_note_id,
                    now=now,
                )
                _versioned_update_row(
                    conn,
                    pending,
                    status="archived",
                    reason="merged_after_review",
                    source_note_id=source_note_id,
                    now=now,
                )
                _add_relation_row(
                    conn,
                    space_id=str(pending["space_id"]),
                    source_memory_id=target_id,
                    target_memory_id=memory_id,
                    relation="derived_from",
                    decision_id=str(decision["id"]),
                    now=now,
                )
                result_id = target_id
                result_ids = [target_id, memory_id]
            elif relation == "update_task" and target is not None:
                for note_id in source_note_ids:
                    _add_source_row(conn, target_id, note_id, "updated_by", now=now)
                _versioned_update_row(
                    conn,
                    target,
                    content=str(pending["content"]),
                    task_status=pending["task_status"],
                    confidence=min(0.99, max(float(target["confidence"]), float(pending["confidence"]))),
                    last_confirmed_at=now,
                    reason="user_approved_task_update",
                    source_note_id=source_note_id,
                    now=now,
                )
                _versioned_update_row(
                    conn,
                    pending,
                    status="archived",
                    reason="task_update_applied_after_review",
                    source_note_id=source_note_id,
                    now=now,
                )
                result_id = target_id
                result_ids = [target_id, memory_id]
            elif relation == "supersede" and target is not None:
                _add_source_row(conn, target_id, source_note_id, "contradicted_by", now=now)
                _versioned_update_row(
                    conn,
                    target,
                    status="superseded",
                    valid_until=now,
                    reason="user_approved_supersede",
                    source_note_id=source_note_id,
                    now=now,
                )
                _versioned_update_row(
                    conn,
                    pending,
                    status="active",
                    last_confirmed_at=now,
                    reason="user_approved_pending_memory",
                    source_note_id=source_note_id,
                    now=now,
                )
                _add_relation_row(
                    conn,
                    space_id=str(pending["space_id"]),
                    source_memory_id=memory_id,
                    target_memory_id=target_id,
                    relation="supersedes",
                    decision_id=str(decision["id"]),
                    now=now,
                )
                _add_relation_row(
                    conn,
                    space_id=str(pending["space_id"]),
                    source_memory_id=target_id,
                    target_memory_id=memory_id,
                    relation="superseded_by",
                    decision_id=str(decision["id"]),
                    now=now,
                )
                result_ids = [memory_id, target_id]
            elif relation == "conflict" and target is not None:
                _add_source_row(conn, target_id, source_note_id, "contradicted_by", now=now)
                _versioned_update_row(
                    conn,
                    target,
                    status="conflicted",
                    reason="user_approved_conflict",
                    source_note_id=source_note_id,
                    now=now,
                )
                _versioned_update_row(
                    conn,
                    pending,
                    status="conflicted",
                    reason="user_approved_conflict",
                    source_note_id=source_note_id,
                    now=now,
                )
                _add_relation_row(
                    conn,
                    space_id=str(pending["space_id"]),
                    source_memory_id=memory_id,
                    target_memory_id=target_id,
                    relation="conflicts_with",
                    decision_id=str(decision["id"]),
                    now=now,
                )
                _add_relation_row(
                    conn,
                    space_id=str(pending["space_id"]),
                    source_memory_id=target_id,
                    target_memory_id=memory_id,
                    relation="conflicts_with",
                    decision_id=str(decision["id"]),
                    now=now,
                )
                result_ids = [memory_id, target_id]
            else:
                raise ValueError(f"unsupported pending review relation: {relation}")

            conn.execute(
                """
                UPDATE memory_decisions
                SET status = 'applied', recommended_action = ?, result_memory_ids_json = ?,
                    reason = reason || '; user_approved', applied_at = ?, error = NULL
                WHERE id = ?
                """,
                (
                    {
                        "new": "insert",
                        "merge": "merge",
                        "update_task": "update_task",
                        "supersede": "supersede",
                        "conflict": "conflict",
                    }[relation],
                    json.dumps(result_ids, ensure_ascii=False),
                    now,
                    decision["id"],
                ),
            )
            return result_id

    result_id = _run_write(_operation)
    return get_memory(result_id, db_path=db_path) if result_id else None


def reject_pending_memory(memory_id: str, *, reason: str = "user_rejected_pending_memory", db_path: str | Path | None = None) -> MemoryRecord | None:
    pending = get_memory(memory_id, db_path=db_path)
    if pending is None or pending.status != "pending_review":
        return None
    updated = update_memory(memory_id, status="archived", reason=reason, db_path=db_path)
    candidate_id = None
    with _connect(db_path) as conn:
        decision = conn.execute(
            "SELECT candidate_id FROM memory_decisions WHERE status = 'pending_review' AND result_memory_ids_json LIKE ? ORDER BY created_at DESC LIMIT 1",
            (f'%"{memory_id}"%',),
        ).fetchone()
        if decision is not None:
            candidate_id = str(decision["candidate_id"])
            conn.execute(
                "UPDATE memory_decisions SET status = 'rejected', error = ? WHERE candidate_id = ? AND status = 'pending_review'",
                (reason, candidate_id),
            )
    if candidate_id:
        mark_memory_candidate(candidate_id, "discarded", error=reason, db_path=db_path)
    return updated


def edit_pending_memory(memory_id: str, content: str, db_path: str | Path | None = None) -> MemoryRecord | None:
    pending = get_memory(memory_id, db_path=db_path)
    if pending is None or pending.status != "pending_review" or not content.strip():
        return None
    update_memory(memory_id, content=content, status="pending_review", reason="user_edited_pending_memory", db_path=db_path)
    return approve_pending_memory(memory_id, db_path=db_path)


def resolve_memory_conflict(
    memory_id: str,
    *,
    resolution: str,
    content: str | None = None,
    db_path: str | Path | None = None,
) -> MemoryRecord | None:
    if resolution not in {"keep", "merge", "archive"}:
        raise ValueError("resolution must be keep, merge, or archive")
    if resolution == "keep":
        return update_memory(memory_id, status="active", reason="user_resolved_conflict_keep", db_path=db_path)
    if resolution == "merge":
        if not content or not content.strip():
            raise ValueError("merge resolution requires content")
        return update_memory(memory_id, content=content, status="active", reason="user_resolved_conflict_merge", db_path=db_path)
    return update_memory(memory_id, status="archived", reason="user_resolved_conflict_archive", db_path=db_path)


def list_memory_decisions(
    space_id: str,
    *,
    note_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    init_db(db_path)
    clauses = ["space_id = ?"]
    params: list[Any] = [space_id]
    if note_id:
        clauses.append("note_id = ?")
        params.append(note_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    params.append(max(1, min(int(limit), 200)))
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM memory_decisions WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            params,
        ).fetchall()
    return [
        {
            "id": row["id"],
            "space_id": row["space_id"],
            "note_id": row["note_id"],
            "candidate_id": row["candidate_id"],
            "relation": row["relation"],
            "target_memory_ids": json.loads(row["target_memory_ids_json"] or "[]"),
            "confidence": float(row["confidence"]),
            "reason": row["reason"],
            "evidence": json.loads(row["evidence_json"] or "[]"),
            "recommended_action": row["recommended_action"],
            "status": row["status"],
            "result_memory_ids": json.loads(row["result_memory_ids_json"] or "[]"),
            "error": row["error"],
            "created_at": row["created_at"],
            "applied_at": row["applied_at"],
        }
        for row in rows
    ]


def list_memory_relations(
    memory_id: str,
    *,
    db_path: str | Path | None = None,
) -> list[MemoryRelation]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, space_id, source_memory_id, target_memory_id, relation, decision_id, created_at
            FROM memory_relations
            WHERE source_memory_id = ? OR target_memory_id = ?
            ORDER BY created_at
            """,
            (memory_id, memory_id),
        ).fetchall()
    return [
        MemoryRelation(
            id=str(row["id"]),
            space_id=str(row["space_id"]),
            source_memory_id=str(row["source_memory_id"]),
            target_memory_id=str(row["target_memory_id"]),
            relation=str(row["relation"]),
            decision_id=row["decision_id"],
            created_at=str(row["created_at"]),
        )
        for row in rows
    ]


def add_memory_relation(
    space_id: str,
    source_memory_id: str,
    target_memory_id: str,
    relation: str,
    *,
    decision_id: str | None = None,
    db_path: str | Path | None = None,
) -> None:
    init_db(db_path)

    def _operation() -> None:
        with _connect(db_path) as conn:
            _add_relation_row(
                conn,
                space_id=space_id,
                source_memory_id=source_memory_id,
                target_memory_id=target_memory_id,
                relation=relation,
                decision_id=decision_id,
                now=utc_now_iso(),
            )

    _run_write(_operation)


def save_memory_trace(trace: dict[str, Any], db_path: str | Path | None = None) -> None:
    init_db(db_path)

    def _operation() -> None:
        with _connect(db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO memory_traces(
                    trace_id, space_id, note_id, trace_type, status, payload_json, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace.get("trace_id"),
                    trace.get("space_id"),
                    trace.get("note_id"),
                    trace.get("trace_type"),
                    trace.get("status") or "unknown",
                    json.dumps(trace, ensure_ascii=False),
                    trace.get("started_at") or utc_now_iso(),
                    trace.get("finished_at"),
                ),
            )

    _run_write(_operation)


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


def mark_extraction_empty_attempt(
    note_id: str,
    space_id: str,
    db_path: str | Path | None = None,
) -> MemoryExtractionState:
    return _mark_extraction_state(note_id, space_id, "empty", increment_attempt=True, db_path=db_path)


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
        decision_rows = conn.execute(
            "SELECT relation, status, COUNT(*) AS count FROM memory_decisions WHERE space_id = ? GROUP BY relation, status",
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
    decisions_by_relation: dict[str, int] = {}
    decisions_by_status: dict[str, int] = {}
    for row in decision_rows:
        count = int(row["count"])
        relation = str(row["relation"])
        decision_status = str(row["status"])
        decisions_by_relation[relation] = decisions_by_relation.get(relation, 0) + count
        decisions_by_status[decision_status] = decisions_by_status.get(decision_status, 0) + count
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
        "decisions_by_relation": decisions_by_relation,
        "decisions_by_status": decisions_by_status,
        "consolidation_last_runs": consolidation_last_runs,
    }


def schema_tables(db_path: str | Path | None = None) -> set[str]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}


from core.settings import STORAGE_BACKEND as _STORAGE_BACKEND

if _STORAGE_BACKEND == "postgres":
    from repositories.postgres import memory as _postgres_memory

    _POSTGRES_EXPORTS = (
        "init_db",
        "add_source",
        "insert_memory",
        "save_memory_candidate",
        "get_memory_candidate",
        "get_memory_candidate_status",
        "mark_memory_candidate",
        "list_retryable_memory_candidates",
        "get_memory",
        "list_memories",
        "list_adjudication_candidates",
        "hybrid_adjudication_candidates",
        "hybrid_search_memories",
        "expire_due_memories",
        "update_memory",
        "apply_memory_decision",
        "mark_accessed",
        "flush_access_counts",
        "soft_delete_memory",
        "correct_memory",
        "purge_memory",
        "approve_pending_memory",
        "reject_pending_memory",
        "edit_pending_memory",
        "resolve_memory_conflict",
        "list_memory_decisions",
        "list_memory_relations",
        "add_memory_relation",
        "save_memory_trace",
        "note_has_memory",
        "get_extraction_state",
        "mark_extraction_processing",
        "mark_extraction_completed",
        "mark_extraction_empty",
        "mark_extraction_empty_attempt",
        "mark_extraction_partial",
        "mark_extraction_failed",
        "list_retryable_extraction_states",
        "consolidation_period_key",
        "reserve_consolidation_run",
        "get_consolidation_run",
        "mark_consolidation_completed",
        "mark_consolidation_failed",
        "search_memories",
        "stats",
        "schema_tables",
    )
    globals().update({name: getattr(_postgres_memory, name) for name in _POSTGRES_EXPORTS})
