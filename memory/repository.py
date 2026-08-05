"""文件作用：本地 SQLite Memory repository。

项目关系：本文件依赖 `core.settings`、`memory.canonicalizer`、`memory.models`、`memory.policies` 等 8 个模块；被 `apps.scheduler`、`core.worker`、`eval.eval_memory`、`eval.eval_memory_quality` 等 30 个模块。
"""



from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
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
    MEMORY_KEY_VERSION,
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
from memory.field_contracts import normalize_task_status

DB_PATH = Path("data/memory/memory.db")
T = TypeVar("T")
_SCHEMA_LOCK = threading.RLock()


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """函数功能：`_connect` 负责连接，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `sqlite3.Connection` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
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
    """函数功能：`_is_locked_error` 负责判断是否为 locked error，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        exc: 当前捕获的异常对象，类型为 `Exception`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).casefold()


def _run_write(operation: Callable[[], T], *, max_attempts: int | None = None) -> T:
    """函数功能：`_run_write` 负责运行 write，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        operation: operation 参数，由调用方传入，类型为 `Callable[[], T]`。
        max_attempts: max attempts 参数，由调用方传入，类型为 `int | None`，默认值为 `None`。
    返回结果说明：
        返回 `T` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
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
    """函数功能：`_parse_iso` 负责解析 iso，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        value: 待转换、校验或计算的值，类型为 `str | None`。
    返回结果说明：
        返回 `datetime | None`；未命中或无需处理时可返回 `None`。
    """
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
    """函数功能：`_is_stale` 负责判断是否为 stale，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        value: 待转换、校验或计算的值，类型为 `str | None`。
        lease_seconds: lease seconds 参数，由调用方传入，类型为 `int`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    parsed = _parse_iso(value)
    if parsed is None:
        return True
    return (datetime.now().astimezone() - parsed).total_seconds() > lease_seconds


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """函数功能：`_ensure_column` 负责确保 column，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        conn: 数据库或 Redis 连接对象，类型为 `sqlite3.Connection`。
        table: table 参数，由调用方传入，类型为 `str`。
        column: column 参数，由调用方传入，类型为 `str`。
        definition: definition 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db(db_path: str | Path | None = None) -> None:
    """函数功能：`init_db` 负责处理 init db，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    with _SCHEMA_LOCK:
        _init_db(db_path)


def _init_db(db_path: str | Path | None = None) -> None:
    """函数功能：`_init_db` 负责处理 init db，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
                memory_key_version TEXT NOT NULL DEFAULT 'memory-key-v2',
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
                memory_key_version TEXT NOT NULL DEFAULT 'memory-key-v2',
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
                clause_index INTEGER,
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
                dimension INTEGER,
                content_hash TEXT,
                embedding_version TEXT,
                status TEXT NOT NULL DEFAULT 'ready',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT,
                last_error TEXT,
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
        _ensure_column(conn, "memories", "memory_key_version", "TEXT NOT NULL DEFAULT 'memory-key-v2'")
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
        _ensure_column(conn, "memory_candidates", "memory_key_version", "TEXT NOT NULL DEFAULT 'memory-key-v2'")
        _ensure_column(conn, "memory_candidates", "clause_index", "INTEGER")
        _ensure_column(conn, "memory_vectors", "dimension", "INTEGER")
        _ensure_column(conn, "memory_vectors", "content_hash", "TEXT")
        _ensure_column(conn, "memory_vectors", "embedding_version", "TEXT")
        _ensure_column(conn, "memory_vectors", "status", "TEXT NOT NULL DEFAULT 'ready'")
        _ensure_column(conn, "memory_vectors", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "memory_vectors", "next_retry_at", "TEXT")
        _ensure_column(conn, "memory_vectors", "last_error", "TEXT")


def _memory_from_row(row: sqlite3.Row, *, sources: list[MemorySource] | None = None, versions: list[MemoryVersion] | None = None) -> MemoryRecord:
    """函数功能：`_memory_from_row` 负责处理 memory from row，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        row: row 参数，由调用方传入，类型为 `sqlite3.Row`。
        sources: sources 参数，由调用方传入，类型为 `list[MemorySource] | None`，默认值为 `None`。
        versions: versions 参数，由调用方传入，类型为 `list[MemoryVersion] | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
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
        # 旧四态数据只在内存中投影，绝不在读取时回写用户数据。
        task_status=normalize_task_status(row["task_status"]),
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
        memory_key_version=row["memory_key_version"] if "memory_key_version" in row.keys() else MEMORY_KEY_VERSION,
        polarity=row["polarity"] or None,
        scope=scope if isinstance(scope, dict) else {},
        sources=sources or [],
        versions=versions or [],
    )


def _candidate_from_row(row: sqlite3.Row) -> MemoryCandidate:
    """函数功能：`_candidate_from_row` 负责处理 candidate from row，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        row: row 参数，由调用方传入，类型为 `sqlite3.Row`。
    返回结果说明：
        返回 `MemoryCandidate` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
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
        task_status=normalize_task_status(row["task_status"]),
        candidate_id=str(row["candidate_id"]),
        note_id=str(row["note_id"]),
        space_id=str(row["space_id"]),
        subject=row["subject"],
        predicate=row["predicate"],
        object_value=row["object_value"],
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
        evidence_span=row["evidence_span"],
        clause_index=row["clause_index"] if "clause_index" in row.keys() else None,
        memory_key=row["memory_key"],
        memory_key_version=row["memory_key_version"] if "memory_key_version" in row.keys() else MEMORY_KEY_VERSION,
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
    """函数功能：`save_memory_candidate` 负责保存 memory candidate，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        status: status 参数，由调用方传入，类型为 `str`，默认值为 `'extracted'`。
        error: 当前捕获的异常对象，类型为 `str | None`，默认值为 `None`。
        decision_id: decision id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryCandidate` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    init_db(db_path)
    now = utc_now_iso()

    def _operation() -> None:
        """函数功能：`_operation` 负责处理 operation，服务于本文件职责：本地 SQLite Memory repository。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
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
                        memory_key, memory_key_version, subject, predicate, object_value, task_status, polarity,
                        scope_json, entities_json, valid_from, valid_until, confidence, importance,
                        evidence_span, clause_index, should_store, extractor_type, extractor_version, model,
                        prompt_hash, status, attempt_count, last_error, decision_id, created_at, updated_at, applied_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        candidate.candidate_id,
                        space_id,
                        candidate.note_id or "",
                        candidate.memory_type,
                        candidate.content,
                        candidate.normalized_content,
                        candidate.effective_memory_key,
                        candidate.memory_key_version,
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
                        candidate.clause_index,
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
    """函数功能：`get_memory_candidate` 负责获取 memory candidate，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        candidate_id: candidate id 参数，由调用方传入，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryCandidate | None`；未命中或无需处理时可返回 `None`。
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM memory_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
    return _candidate_from_row(row) if row is not None else None


def get_memory_candidate_status(candidate_id: str, db_path: str | Path | None = None) -> str | None:
    """函数功能：`get_memory_candidate_status` 负责获取 memory candidate status，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        candidate_id: candidate id 参数，由调用方传入，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
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
    """函数功能：`mark_memory_candidate` 负责标记 memory candidate，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        candidate_id: candidate id 参数，由调用方传入，类型为 `str`。
        status: status 参数，由调用方传入，类型为 `str`。
        error: 当前捕获的异常对象，类型为 `str | None`，默认值为 `None`。
        decision_id: decision id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    allowed = {"extracted", "validated", "adjudicated", "applied", "pending_review", "discarded", "failed", "processing"}
    if status not in allowed:
        raise ValueError(f"invalid memory candidate status: {status}")
    init_db(db_path)
    now = utc_now_iso()

    def _operation() -> bool:
        """函数功能：`_operation` 负责处理 operation，服务于本文件职责：本地 SQLite Memory repository。
        传参：
            无。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
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
    """函数功能：`list_retryable_memory_candidates` 负责列出 retryable memory candidates，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `100`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[MemoryCandidate]`，表示按条件筛选、构造或查询得到的列表。
    """
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
    """函数功能：`_extraction_state_from_row` 负责处理 extraction state from row，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        row: row 参数，由调用方传入，类型为 `sqlite3.Row`。
    返回结果说明：
        返回 `MemoryExtractionState` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
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
    """函数功能：`_consolidation_run_from_row` 负责运行 from row，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        row: row 参数，由调用方传入，类型为 `sqlite3.Row`。
    返回结果说明：
        返回 `ConsolidationRun` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
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
    """函数功能：`_load_sources` 负责加载 sources，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        conn: 数据库或 Redis 连接对象，类型为 `sqlite3.Connection`。
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
    返回结果说明：
        返回 `list[MemorySource]`，表示按条件筛选、构造或查询得到的列表。
    """
    rows = conn.execute(
        "SELECT memory_id, note_id, relation, created_at FROM memory_sources WHERE memory_id = ? ORDER BY created_at",
        (memory_id,),
    ).fetchall()
    return [MemorySource(memory_id=row["memory_id"], note_id=row["note_id"], relation=row["relation"], created_at=row["created_at"]) for row in rows]


def _load_versions(conn: sqlite3.Connection, memory_id: str) -> list[MemoryVersion]:
    """函数功能：`_load_versions` 负责加载 versions，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        conn: 数据库或 Redis 连接对象，类型为 `sqlite3.Connection`。
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
    返回结果说明：
        返回 `list[MemoryVersion]`，表示按条件筛选、构造或查询得到的列表。
    """
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
            task_status=normalize_task_status(row["task_status"]),
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
    """函数功能：`_add_version` 负责处理 add version，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        conn: 数据库或 Redis 连接对象，类型为 `sqlite3.Connection`。
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        version: version 参数，由调用方传入，类型为 `int`。
        content: 需要处理、保存或展示的文本内容，类型为 `str`。
        status: status 参数，由调用方传入，类型为 `str`。
        task_status: task status 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        confidence: confidence 参数，由调用方传入，类型为 `float | None`，默认值为 `None`。
        importance: importance 参数，由调用方传入，类型为 `float | None`，默认值为 `None`。
        valid_from: valid from 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        valid_until: valid until 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        reason: reason 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        source_note_id: source note id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`_add_source_row` 负责处理 add source row，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        conn: 数据库或 Redis 连接对象，类型为 `sqlite3.Connection`。
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        relation: relation 参数，由调用方传入，类型为 `str`。
        now: now 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
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
    """函数功能：`_insert_memory_row` 负责处理 insert memory row，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        conn: 数据库或 Redis 连接对象，类型为 `sqlite3.Connection`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        source_note_id: source note id 参数，由调用方传入，类型为 `str`。
        source_relation: source relation 参数，由调用方传入，类型为 `str`，默认值为 `'created_from'`。
        status: status 参数，由调用方传入，类型为 `str`，默认值为 `'active'`。
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str | None`，默认值为 `None`。
        now: now 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    if status not in MEMORY_STATUSES:
        raise ValueError(f"invalid memory status: {status}")
    created_at = now or utc_now_iso()
    record_id = memory_id or new_id("mem")
    valid_from = candidate.valid_from
    conn.execute(
        """
        INSERT INTO memories(
            id, space_id, memory_type, content, normalized_content, importance, confidence,
            status, task_status, subject, predicate, object_value, memory_key, memory_key_version, polarity, scope_json, valid_from, valid_until,
            last_confirmed_at, created_at, updated_at, last_accessed_at, access_count, current_version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            candidate.memory_key_version,
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
        task_status=normalize_task_status(candidate.task_status),
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
    """函数功能：`_add_relation_row` 负责处理 add relation row，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        conn: 数据库或 Redis 连接对象，类型为 `sqlite3.Connection`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        source_memory_id: source memory id 参数，由调用方传入，类型为 `str`。
        target_memory_id: target memory id 参数，由调用方传入，类型为 `str`。
        relation: relation 参数，由调用方传入，类型为 `str`。
        decision_id: decision id 参数，由调用方传入，类型为 `str | None`。
        now: now 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`_insert_decision_row` 负责处理 insert decision row，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        conn: 数据库或 Redis 连接对象，类型为 `sqlite3.Connection`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        decision: decision 参数，由调用方传入，类型为 `MemoryDecision`。
        status: status 参数，由调用方传入，类型为 `str`。
        result_memory_ids: result memory ids 参数，由调用方传入，类型为 `list[str] | None`，默认值为 `None`。
        error: 当前捕获的异常对象，类型为 `str | None`，默认值为 `None`。
        now: now 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`add_source` 负责处理 add source，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        relation: relation 参数，由调用方传入，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    init_db(db_path)

    def _operation() -> bool:
        """函数功能：`_operation` 负责处理 operation，服务于本文件职责：本地 SQLite Memory repository。
        传参：
            无。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
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
    """函数功能：`insert_memory` 负责处理 insert memory，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        source_note_id: source note id 参数，由调用方传入，类型为 `str`。
        source_relation: source relation 参数，由调用方传入，类型为 `str`，默认值为 `'created_from'`。
        status: status 参数，由调用方传入，类型为 `str`，默认值为 `'active'`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    if status not in MEMORY_STATUSES:
        raise ValueError(f"invalid memory status: {status}")
    init_db(db_path)
    memory_id = new_id("mem")

    def _operation() -> None:
        """函数功能：`_operation` 负责处理 operation，服务于本文件职责：本地 SQLite Memory repository。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
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
    """函数功能：`get_memory` 负责获取 memory，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord | None`；未命中或无需处理时可返回 `None`。
    """
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
    """函数功能：`list_memories` 负责列出 memories，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        status: status 参数，由调用方传入，类型为 `str | None`，默认值为 `'active'`。
        memory_type: memory type 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        memory_key: memory key 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        include_expired: include expired 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `20`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[MemoryRecord]`，表示按条件筛选、构造或查询得到的列表。
    """
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
    """函数功能：`list_adjudication_candidates` 负责列出 adjudication candidates，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        memory_type: memory type 参数，由调用方传入，类型为 `str`。
        memory_key: memory key 参数，由调用方传入，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `200`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[MemoryRecord]`，表示按条件筛选、构造或查询得到的列表。
    """
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
    """函数功能：`hybrid_adjudication_candidates` 负责处理 hybrid adjudication candidates，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        query_embedding: query embedding 参数，由调用方传入，类型为 `list[float] | None`，默认值为 `None`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `20`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[MemoryRecord]`，表示按条件筛选、构造或查询得到的列表。
    """
    del query_embedding
    from memory.canonicalizer import task_identity_compatible
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
        if candidate.memory_type == "task":
            # 旧版 V2 任务可能使用泛化 subject/predicate 槽位，V3 结构化过滤无法命中旧 key；向本地 identity bridge 提供有界任务切片，最终兼容性仍由 Relation Guard 判断。
            structured_rows.extend(
                conn.execute(
                    "SELECT * FROM memories WHERE space_id = ? AND status = 'active' AND memory_type = 'task' "
                    "AND (valid_until IS NULL OR valid_until > ?) ORDER BY updated_at DESC LIMIT ?",
                    (space_id, now, max(30, min(int(limit) * 3, 200))),
                ).fetchall()
            )
        rows_by_id = {row["id"]: row for row in [*exact_rows, *structured_rows]}
        memories = [_memory_from_row(row) for row in rows_by_id.values()]

    def _matches(memory: MemoryRecord) -> bool:
        """函数功能：`_matches` 负责处理 matches，服务于本文件职责：本地 SQLite Memory repository。
        传参：
            memory: memory 参数，由调用方传入，类型为 `MemoryRecord`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        if memory.effective_memory_key == candidate.effective_memory_key:
            return True
        if candidate.memory_type == "task" and task_identity_compatible(candidate, memory):
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
    """函数功能：`expire_due_memories` 负责处理过期状态 due memories，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str | None`，默认值为 `None`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `500`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `int`，表示计算得到的数值结果。
    """
    init_db(db_path)
    now = utc_now_iso()

    def _operation() -> int:
        """函数功能：`_operation` 负责处理 operation，服务于本文件职责：本地 SQLite Memory repository。
        传参：
            无。
        返回结果说明：
            返回 `int`，表示计算得到的数值结果。
        """
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
    """函数功能：`update_memory` 负责更新 memory，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        content: 需要处理、保存或展示的文本内容，类型为 `str | None`，默认值为 `None`。
        status: status 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        task_status: task status 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        valid_until: valid until 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        confidence: confidence 参数，由调用方传入，类型为 `float | None`，默认值为 `None`。
        importance: importance 参数，由调用方传入，类型为 `float | None`，默认值为 `None`。
        last_confirmed_at: last confirmed at 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        reason: reason 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        source_note_id: source note id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord | None`；未命中或无需处理时可返回 `None`。
    """
    init_db(db_path)

    def _operation() -> bool:
        """函数功能：`_operation` 负责处理 operation，服务于本文件职责：本地 SQLite Memory repository。
        传参：
            无。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
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
            next_memory_key = (
                row["memory_key"]
                if row["memory_key_version"] == "memory-key-v3" and row["memory_key"]
                else memory_key_for(
                    str(row["memory_type"]),
                    subject=row["subject"],
                    predicate=row["predicate"],
                    object_value=row["object_value"],
                    content=next_content,
                )
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
    object_value: str | None = None,
    scope: dict[str, Any] | None = None,
    memory_key: str | None = None,
    memory_key_version: str | None = None,
    polarity: str | None = None,
    reason: str,
    source_note_id: str | None,
    now: str,
) -> None:
    """函数功能：`_versioned_update_row` 负责更新 row，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        conn: 数据库或 Redis 连接对象，类型为 `sqlite3.Connection`。
        row: row 参数，由调用方传入，类型为 `sqlite3.Row`。
        content: 需要处理、保存或展示的文本内容，类型为 `str | None`，默认值为 `None`。
        status: status 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        task_status: task status 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        valid_until: valid until 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        confidence: confidence 参数，由调用方传入，类型为 `float | None`，默认值为 `None`。
        importance: importance 参数，由调用方传入，类型为 `float | None`，默认值为 `None`。
        last_confirmed_at: last confirmed at 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        object_value: object value 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        scope: scope 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
        memory_key: memory key 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        memory_key_version: memory key version 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        reason: reason 参数，由调用方传入，类型为 `str`。
        source_note_id: source note id 参数，由调用方传入，类型为 `str | None`。
        now: now 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    next_version = int(row["current_version"] or 1) + 1
    next_content = content if content is not None else str(row["content"])
    next_status = status if status is not None else str(row["status"])
    next_task_status = task_status if task_status is not None else row["task_status"]
    next_valid_until = valid_until if valid_until is not None else row["valid_until"]
    next_confidence = float(confidence) if confidence is not None else float(row["confidence"])
    next_importance = float(importance) if importance is not None else float(row["importance"])
    next_confirmed = last_confirmed_at if last_confirmed_at is not None else row["last_confirmed_at"]
    next_object_value = object_value if object_value is not None else row["object_value"]
    try:
        existing_scope = json.loads(row["scope_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        existing_scope = {}
    next_scope = scope if scope is not None else existing_scope
    next_memory_key = memory_key if memory_key is not None else row["memory_key"]
    next_memory_key_version = memory_key_version if memory_key_version is not None else row["memory_key_version"]
    next_polarity = polarity if polarity is not None else row["polarity"]
    conn.execute(
        """
        UPDATE memories
        SET content = ?, normalized_content = ?, status = ?, task_status = ?, valid_until = ?,
            object_value = ?, memory_key = ?, memory_key_version = ?, polarity = ?, scope_json = ?,
            confidence = ?, importance = ?, last_confirmed_at = ?, updated_at = ?, current_version = ?
        WHERE id = ?
        """,
        (
            next_content,
            normalize_content(next_content),
            next_status,
            next_task_status,
            next_valid_until,
            next_object_value,
            next_memory_key,
            next_memory_key_version,
            next_polarity,
            json.dumps(next_scope, ensure_ascii=False),
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


def _archive_terminal_task_duplicates_row(
    conn: sqlite3.Connection,
    target_row: sqlite3.Row,
    candidate: MemoryCandidate,
    *,
    decision_id: str,
    source_note_id: str,
    now: str,
) -> list[str]:
    """函数功能：`_archive_terminal_task_duplicates_row` 负责归档 terminal task duplicates row，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        conn: 数据库或 Redis 连接对象，类型为 `sqlite3.Connection`。
        target_row: target row 参数，由调用方传入，类型为 `sqlite3.Row`。
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        decision_id: decision id 参数，由调用方传入，类型为 `str`。
        source_note_id: source note id 参数，由调用方传入，类型为 `str`。
        now: now 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
    if candidate.memory_type != "task" or candidate.task_status != "done":
        return []
    from memory.canonicalizer import task_identity_compatible

    rows = conn.execute(
        """
        SELECT * FROM memories
        WHERE space_id = ? AND memory_type = 'task' AND status = 'active' AND id <> ?
        ORDER BY updated_at DESC
        """,
        (str(target_row["space_id"]), str(target_row["id"])),
    ).fetchall()
    archived: list[str] = []
    for row in rows:
        if not task_identity_compatible(candidate, _memory_from_row(row)):
            continue
        _versioned_update_row(
            conn,
            row,
            status="archived",
            reason="duplicate_task_identity_reconciled",
            source_note_id=source_note_id,
            now=now,
        )
        _add_relation_row(
            conn,
            space_id=str(target_row["space_id"]),
            source_memory_id=str(target_row["id"]),
            target_memory_id=str(row["id"]),
            relation="supersedes",
            decision_id=decision_id,
            now=now,
        )
        _add_relation_row(
            conn,
            space_id=str(target_row["space_id"]),
            source_memory_id=str(row["id"]),
            target_memory_id=str(target_row["id"]),
            relation="superseded_by",
            decision_id=decision_id,
            now=now,
        )
        archived.append(str(row["id"]))
    return archived


def apply_memory_decision(
    space_id: str,
    note_id: str,
    candidate: MemoryCandidate,
    decision: MemoryDecision,
    *,
    merged_content: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """函数功能：`apply_memory_decision` 负责处理 apply memory decision，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        decision: decision 参数，由调用方传入，类型为 `MemoryDecision`。
        merged_content: merged content 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    init_db(db_path)

    def _operation() -> dict[str, Any]:
        """函数功能：`_operation` 负责处理 operation，服务于本文件职责：本地 SQLite Memory repository。
        传参：
            无。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
        with _connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT id, relation, recommended_action, status, result_memory_ids_json FROM memory_decisions "
                "WHERE candidate_id = ? AND status IN ('applied', 'pending_review') ORDER BY created_at DESC LIMIT 1",
                (candidate.candidate_id,),
            ).fetchone()
            if prior is not None:
                try:
                    prior_ids = json.loads(prior["result_memory_ids_json"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    prior_ids = []
                replay_action = "pending_review" if prior["status"] == "pending_review" else "add_source"
                replay_relation = "conflict" if prior["status"] == "pending_review" else "same"
                replay = {"action": replay_action, "relation": replay_relation, "decision_id": prior["id"], "candidate_id": candidate.candidate_id, "idempotent": True, "source_added": False, "result_memory_ids": prior_ids}
                if prior_ids:
                    replay["memory_id"] = prior_ids[0]
                return replay
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
            decision_to_write = decision
            if target_row is not None and decision.target_snapshot_version is not None and int(target_row["current_version"] or 1) != int(decision.target_snapshot_version):
                same_identity = candidate.effective_memory_key == str(target_row["memory_key"] or "")
                same_state = same_identity and (
                    (candidate.memory_type == "task" and candidate.task_status == target_row["task_status"])
                    or (candidate.memory_type != "task" and candidate.object_value and candidate.object_value == target_row["object_value"])
                )
                if same_state:
                    decision_to_write = replace(
                        decision,
                        relation="same",
                        recommended_action="add_source",
                        reason="concurrent_same_update_adds_source",
                    )
                    action = "add_source"
                    result.update({"action": action, "relation": "same", "original_action": decision.recommended_action})
                else:
                    decision_to_write = replace(
                        decision,
                        relation="conflict",
                        recommended_action="pending_review",
                        reason="stale_target_snapshot_requires_readjudication",
                    )
                    action = "pending_review"
                    result.update({"action": action, "relation": "conflict", "original_action": decision.recommended_action})
            if action == "update_task" and target_row is not None and candidate.memory_type == "task" and candidate.task_status == "done":
                current_status = normalize_task_status(target_row["task_status"])
                if current_status == "done":
                    action = "add_source"
                    result.update({"action": "add_source", "relation": "same", "original_action": "update_task", "idempotent": True})
            for evidence in decision_to_write.evidence:
                if not evidence.startswith("audit:"):
                    continue
                try:
                    result["audit"] = json.loads(evidence[6:])
                except (TypeError, json.JSONDecodeError):
                    result["audit"] = evidence[6:]

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
                if decision_to_write.target_memory_ids:
                    result["target_memory_ids"] = list(decision_to_write.target_memory_ids)
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
                if source_added:
                    archived = _archive_terminal_task_duplicates_row(
                        conn, target_row, candidate, decision_id=decision.decision_id,
                        source_note_id=note_id, now=now,
                    )
                    if archived:
                        result["archived_duplicate_ids"] = archived
            elif action in {"merge", "update"} and target_row is not None:
                source_added = _add_source_row(conn, target_id, note_id, "updated_by" if action == "update" else "supported_by", now=now)
                if source_added:
                    _versioned_update_row(
                        conn,
                        target_row,
                        content=merged_content or candidate.content,
                        object_value=candidate.object_value,
                        scope=dict(candidate.scope),
                        memory_key=candidate.effective_memory_key,
                        memory_key_version=candidate.memory_key_version,
                        polarity=candidate.polarity,
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
                        task_status=normalize_task_status(candidate.task_status),
                        object_value=candidate.object_value,
                        scope=dict(candidate.scope),
                        memory_key=candidate.effective_memory_key,
                        memory_key_version=candidate.memory_key_version,
                        confidence=min(0.99, max(float(target_row["confidence"]), candidate.confidence)),
                        last_confirmed_at=now,
                        reason=decision.reason,
                        source_note_id=note_id,
                        now=now,
                    )
                result_memory_ids.append(target_id)
                result.update({"memory_id": target_id, "task_status": candidate.task_status, "source_added": source_added})
                if source_added:
                    archived = _archive_terminal_task_duplicates_row(
                        conn, target_row, candidate, decision_id=decision.decision_id,
                        source_note_id=note_id, now=now,
                    )
                    if archived:
                        result["archived_duplicate_ids"] = archived
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
                decision=decision_to_write,
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
            """函数功能：`_record_failure` 负责记录 failure，服务于本文件职责：本地 SQLite Memory repository。
            传参：
                无。
            返回结果说明：
                无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
            """
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
    """函数功能：`mark_accessed` 负责标记 accessed，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        memory_ids: memory ids 参数，由调用方传入，类型为 `list[str]`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    if not memory_ids:
        return
    init_db(db_path)
    now = utc_now_iso()

    def _operation() -> None:
        """函数功能：`_operation` 负责处理 operation，服务于本文件职责：本地 SQLite Memory repository。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        with _connect(db_path) as conn:
            conn.executemany(
                "UPDATE memories SET last_accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
                [(now, memory_id) for memory_id in memory_ids],
            )

    _run_write(_operation)


def flush_access_counts(*, limit: int = 1000, db_path: str | Path | None = None) -> int:
    """函数功能：`flush_access_counts` 负责处理 flush access counts，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `1000`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `int`，表示计算得到的数值结果。
    """
    del limit, db_path
    return 0


def soft_delete_memory(memory_id: str, *, reason: str = "user_forget", db_path: str | Path | None = None) -> MemoryRecord | None:
    """函数功能：`soft_delete_memory` 负责软删除 delete memory，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        reason: reason 参数，由调用方传入，类型为 `str`，默认值为 `'user_forget'`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord | None`；未命中或无需处理时可返回 `None`。
    """
    return update_memory(memory_id, status="deleted", reason=reason, db_path=db_path)


def correct_memory(
    memory_id: str,
    content: str,
    *,
    task_status: str | None = None,
    reason: str = "user_correct",
    db_path: str | Path | None = None,
) -> MemoryRecord | None:
    """函数功能：`correct_memory` 负责处理 correct memory，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        content: 需要处理、保存或展示的文本内容，类型为 `str`。
        task_status: task status 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        reason: reason 参数，由调用方传入，类型为 `str`，默认值为 `'user_correct'`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord | None`；未命中或无需处理时可返回 `None`。
    """
    existing = get_memory(memory_id, db_path=db_path)
    if existing is None:
        return None
    from memory.task_state import infer_task_status, validate_task_status
    from memory.policies.task import can_transition

    resolved_status = validate_task_status(task_status)
    if existing.memory_type == "task":
        resolved_status = resolved_status or infer_task_status(content) or existing.task_status
        if resolved_status != existing.task_status and not can_transition(existing.task_status, resolved_status):
            raise ValueError(f"invalid task status transition: {existing.task_status} -> {resolved_status}")
        if existing.task_status == "done" and resolved_status == "todo":
            reopen_markers = ("重新", "再次", "重做", "返工", "恢复", "再开始", "又开始")
            if not any(marker in content for marker in reopen_markers):
                raise ValueError("reopening a terminal task requires explicit wording")
    return update_memory(
        memory_id,
        content=content,
        status="active",
        task_status=resolved_status if existing.memory_type == "task" else None,
        reason=reason,
        db_path=db_path,
    )


def purge_memory(memory_id: str, db_path: str | Path | None = None) -> bool:
    """函数功能：`purge_memory` 负责彻底清除 memory，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    init_db(db_path)

    def _operation() -> bool:
        """函数功能：`_operation` 负责处理 operation，服务于本文件职责：本地 SQLite Memory repository。
        传参：
            无。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
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
    """函数功能：`approve_pending_memory` 负责批准 pending memory，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord | None`；未命中或无需处理时可返回 `None`。
    """
    init_db(db_path)

    def _operation() -> str | None:
        """函数功能：`_operation` 负责处理 operation，服务于本文件职责：本地 SQLite Memory repository。
        传参：
            无。
        返回结果说明：
            返回 `str | None`；未命中或无需处理时可返回 `None`。
        """
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
    """函数功能：`reject_pending_memory` 负责拒绝 pending memory，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        reason: reason 参数，由调用方传入，类型为 `str`，默认值为 `'user_rejected_pending_memory'`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord | None`；未命中或无需处理时可返回 `None`。
    """
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
    """函数功能：`edit_pending_memory` 负责处理 edit pending memory，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        content: 需要处理、保存或展示的文本内容，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord | None`；未命中或无需处理时可返回 `None`。
    """
    pending = get_memory(memory_id, db_path=db_path)
    if pending is None or pending.status != "pending_review" or not content.strip():
        return None
    from memory.task_state import infer_task_status
    from memory.policies.task import can_transition

    next_status = infer_task_status(content) if pending.memory_type == "task" else None
    update_memory(
        memory_id,
        content=content,
        status="pending_review",
        task_status=normalize_task_status(next_status if next_status is not None else pending.task_status) if pending.memory_type == "task" else None,
        reason="user_edited_pending_memory",
        db_path=db_path,
    )
    with _connect(db_path) as conn:
        decision = conn.execute(
            "SELECT * FROM memory_decisions WHERE status = 'pending_review' AND result_memory_ids_json LIKE ? ORDER BY created_at DESC LIMIT 1",
            (f'%"{memory_id}"%',),
        ).fetchone()
        if decision is not None and pending.memory_type == "task" and next_status is not None:
            target_ids = json.loads(decision["target_memory_ids_json"] or "[]")
            target = get_memory(str(target_ids[0]), db_path=db_path) if target_ids else None
            if target is not None and (next_status == target.task_status or can_transition(target.task_status, next_status)):
                conn.execute(
                    "UPDATE memory_decisions SET relation = 'update_task', recommended_action = 'update_task', reason = ?, target_snapshot_version = ? WHERE id = ?",
                    ("user_edited_pending_memory_revalidated", target.current_version, decision["id"]),
                )
            candidate = conn.execute("SELECT candidate_id FROM memory_candidates WHERE candidate_id = ?", (decision["candidate_id"],)).fetchone()
            if candidate is not None:
                conn.execute(
                    "UPDATE memory_candidates SET content = ?, normalized_content = ?, task_status = ?, updated_at = ? WHERE candidate_id = ?",
                    (content, normalize_content(content), next_status, utc_now_iso(), decision["candidate_id"]),
                )
    return approve_pending_memory(memory_id, db_path=db_path)


def resolve_memory_conflict(
    memory_id: str,
    *,
    resolution: str,
    content: str | None = None,
    db_path: str | Path | None = None,
) -> MemoryRecord | None:
    """函数功能：`resolve_memory_conflict` 负责解析 memory conflict，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        resolution: resolution 参数，由调用方传入，类型为 `str`。
        content: 需要处理、保存或展示的文本内容，类型为 `str | None`，默认值为 `None`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryRecord | None`；未命中或无需处理时可返回 `None`。
    """
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
    """函数功能：`list_memory_decisions` 负责列出 memory decisions，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str | None`，默认值为 `None`。
        status: status 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `50`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
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
    """函数功能：`list_memory_relations` 负责列出 memory relations，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[MemoryRelation]`，表示按条件筛选、构造或查询得到的列表。
    """
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
    """函数功能：`add_memory_relation` 负责处理 add memory relation，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        source_memory_id: source memory id 参数，由调用方传入，类型为 `str`。
        target_memory_id: target memory id 参数，由调用方传入，类型为 `str`。
        relation: relation 参数，由调用方传入，类型为 `str`。
        decision_id: decision id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    init_db(db_path)

    def _operation() -> None:
        """函数功能：`_operation` 负责处理 operation，服务于本文件职责：本地 SQLite Memory repository。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
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
    """函数功能：`save_memory_trace` 负责保存 memory trace，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        trace: trace 参数，由调用方传入，类型为 `dict[str, Any]`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    init_db(db_path)

    def _operation() -> None:
        """函数功能：`_operation` 负责处理 operation，服务于本文件职责：本地 SQLite Memory repository。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
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
    """函数功能：`note_has_memory` 负责判断是否包含 memory，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT 1 FROM memory_sources WHERE note_id = ? LIMIT 1", (note_id,)).fetchone()
        return row is not None


def get_extraction_state(note_id: str, db_path: str | Path | None = None) -> MemoryExtractionState | None:
    """函数功能：`get_extraction_state` 负责获取 extraction state，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryExtractionState | None`；未命中或无需处理时可返回 `None`。
    """
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
    """函数功能：`_mark_extraction_state` 负责标记 extraction state，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        status: status 参数，由调用方传入，类型为 `str`。
        candidate_count: candidate count 参数，由调用方传入，类型为 `int`，默认值为 `0`。
        processed_count: processed count 参数，由调用方传入，类型为 `int`，默认值为 `0`。
        error: 当前捕获的异常对象，类型为 `str | None`，默认值为 `None`。
        increment_attempt: increment attempt 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryExtractionState` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    if status not in MEMORY_EXTRACTION_STATUSES:
        raise ValueError(f"invalid memory extraction status: {status}")
    init_db(db_path)
    now = utc_now_iso()
    started_at = now if status == "processing" else None
    completed_at = now if status in {"completed", "empty", "partial", "failed"} else None
    attempt_delta = 1 if increment_attempt else 0

    def _operation() -> None:
        """函数功能：`_operation` 负责处理 operation，服务于本文件职责：本地 SQLite Memory repository。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
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
    """函数功能：`mark_extraction_processing` 负责标记 extraction processing，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryExtractionState` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return _mark_extraction_state(note_id, space_id, "processing", increment_attempt=True, db_path=db_path)


def mark_extraction_completed(
    note_id: str,
    space_id: str,
    *,
    candidate_count: int,
    processed_count: int,
    db_path: str | Path | None = None,
) -> MemoryExtractionState:
    """函数功能：`mark_extraction_completed` 负责标记 extraction completed，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        candidate_count: candidate count 参数，由调用方传入，类型为 `int`。
        processed_count: processed count 参数，由调用方传入，类型为 `int`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryExtractionState` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return _mark_extraction_state(
        note_id,
        space_id,
        "completed",
        candidate_count=candidate_count,
        processed_count=processed_count,
        db_path=db_path,
    )


def mark_extraction_empty(note_id: str, space_id: str, db_path: str | Path | None = None) -> MemoryExtractionState:
    """函数功能：`mark_extraction_empty` 负责标记 extraction empty，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryExtractionState` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return _mark_extraction_state(note_id, space_id, "empty", db_path=db_path)


def mark_extraction_empty_attempt(
    note_id: str,
    space_id: str,
    db_path: str | Path | None = None,
) -> MemoryExtractionState:
    """函数功能：`mark_extraction_empty_attempt` 负责标记 extraction empty attempt，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryExtractionState` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
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
    """函数功能：`mark_extraction_partial` 负责标记 extraction partial，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        candidate_count: candidate count 参数，由调用方传入，类型为 `int`。
        processed_count: processed count 参数，由调用方传入，类型为 `int`。
        error: 当前捕获的异常对象，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryExtractionState` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
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
    """函数功能：`mark_extraction_failed` 负责标记 extraction failed，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        error: 当前捕获的异常对象，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryExtractionState` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return _mark_extraction_state(note_id, space_id, "failed", error=error, db_path=db_path)


def list_retryable_extraction_states(
    space_id: str,
    *,
    limit: int = 100,
    db_path: str | Path | None = None,
) -> list[MemoryExtractionState]:
    """函数功能：`list_retryable_extraction_states` 负责列出 retryable extraction states，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `100`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[MemoryExtractionState]`，表示按条件筛选、构造或查询得到的列表。
    """
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
    """函数功能：`consolidation_period_key` 负责处理 consolidation period key，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        cadence: cadence 参数，由调用方传入，类型为 `str`。
        day: day 参数，由调用方传入，类型为 `date`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
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
    """函数功能：`reserve_consolidation_run` 负责预约 consolidation run，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        cadence: cadence 参数，由调用方传入，类型为 `str`。
        period_key: period key 参数，由调用方传入，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `ConsolidationRun | None`；未命中或无需处理时可返回 `None`。
    """
    cadence = cadence.strip().lower()
    init_db(db_path)

    def _operation() -> str | None:
        """函数功能：`_operation` 负责处理 operation，服务于本文件职责：本地 SQLite Memory repository。
        传参：
            无。
        返回结果说明：
            返回 `str | None`；未命中或无需处理时可返回 `None`。
        """
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
    """函数功能：`get_consolidation_run` 负责获取 consolidation run，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        run_id: run id 参数，由调用方传入，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `ConsolidationRun | None`；未命中或无需处理时可返回 `None`。
    """
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM memory_consolidation_runs WHERE id = ?", (run_id,)).fetchone()
    return _consolidation_run_from_row(row) if row is not None else None


def mark_consolidation_completed(run_id: str, result: dict[str, Any], db_path: str | Path | None = None) -> None:
    """函数功能：`mark_consolidation_completed` 负责标记 consolidation completed，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        run_id: run id 参数，由调用方传入，类型为 `str`。
        result: 上游步骤返回的结果对象，类型为 `dict[str, Any]`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    init_db(db_path)
    now = utc_now_iso()
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True)

    def _operation() -> None:
        """函数功能：`_operation` 负责处理 operation，服务于本文件职责：本地 SQLite Memory repository。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
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
    """函数功能：`mark_consolidation_failed` 负责标记 consolidation failed，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        run_id: run id 参数，由调用方传入，类型为 `str`。
        error: 当前捕获的异常对象，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    init_db(db_path)
    now = utc_now_iso()

    def _operation() -> None:
        """函数功能：`_operation` 负责处理 operation，服务于本文件职责：本地 SQLite Memory repository。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
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
    access_context: Any = None,
) -> list[tuple[MemoryRecord, float]]:
    """函数功能：`search_memories` 负责搜索 memories，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        query: 检索或查询文本，类型为 `str`。
        memory_type: memory type 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        include_inactive: include inactive 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
        min_score: min score 参数，由调用方传入，类型为 `float`，默认值为 `MEMORY_QUERY_MIN_SCORE`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `10`。
        mark_access: mark access 参数，由调用方传入，类型为 `bool`，默认值为 `True`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[tuple[MemoryRecord, float]]`，表示按条件筛选、构造或查询得到的列表。
    """
    from memory.retriever import score_memory

    candidates = list_memories(
        space_id,
        status=None if include_inactive else "active",
        memory_type=memory_type,
        limit=100,
        db_path=db_path,
    )
    if access_context is not None:
        from memory.access import memory_access_allowed
        candidates = [memory for memory in candidates if memory_access_allowed(memory, access_context)]
    scored = [(memory, score_memory(query, memory)) for memory in candidates]
    scored = [(memory, score) for memory, score in scored if score >= min_score]
    scored.sort(key=lambda item: item[1], reverse=True)
    limited = scored[: max(1, min(int(limit), 50))]
    if mark_access:
        mark_accessed([memory.id for memory, _score in limited], db_path=db_path)
    return limited


def stats(space_id: str, db_path: str | Path | None = None) -> dict[str, Any]:
    """函数功能：`stats` 负责处理 stats，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
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


def get_memory_timeline(
    space_id: str,
    *,
    memory_id: str | None = None,
    query: str | None = None,
    limit: int = 10,
    access_context: Any = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """SQLite-compatible history timeline API."""
    init_db(db_path)
    with _connect(db_path) as conn:
        clauses = ["space_id = ?"]
        params: list[Any] = [space_id]
        if memory_id:
            clauses.append("id = ?")
            params.append(memory_id)
        elif query:
            clauses.append("(normalized_content = ? OR memory_key = ? OR content LIKE ?)")
            params.extend([normalize_content(query), query.strip(), f"%{query.strip()[:160]}%"])
        rows = conn.execute(
            f"SELECT * FROM memories WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, id DESC LIMIT ?",
            [*params, max(1, min(int(limit), 50))],
        ).fetchall()
        result = []
        for row in rows:
            scope = json.loads(row["scope_json"] or "{}")
            from memory.access import memory_access_allowed
            if access_context is not None and not memory_access_allowed({"scope": scope}, access_context):
                continue
            sources = conn.execute("SELECT * FROM memory_sources WHERE memory_id = ? ORDER BY created_at", (row["id"],)).fetchall()
            result.append({
                "id": row["id"],
                "memory_id": row["id"],
                "memory_type": row["memory_type"],
                "memory_key": row["memory_key"],
                "content": row["content"],
                "status": row["status"],
                "task_status": normalize_task_status(row["task_status"]),
                "current_version": row["current_version"],
                "updated_at": row["updated_at"],
                "sources": [dict(x) for x in sources],
                "versions": [version.__dict__ for version in _load_versions(conn, row["id"])],
            })
        return result


def schema_tables(db_path: str | Path | None = None) -> set[str]:
    """函数功能：`schema_tables` 负责处理 schema tables，服务于本文件职责：本地 SQLite Memory repository。
    传参：
        db_path: db path 参数，由调用方传入，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `set[str]` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
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
        "get_memory_timeline",
        "stats",
        "schema_tables",
    )
    globals().update({name: getattr(_postgres_memory, name) for name in _POSTGRES_EXPORTS})
