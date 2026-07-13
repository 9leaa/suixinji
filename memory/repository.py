"""SQLite repository for Memory V2."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from memory.models import (
    MEMORY_STATUSES,
    MemoryCandidate,
    MemoryRecord,
    MemorySource,
    MemoryVersion,
    new_id,
    normalize_content,
    utc_now_iso,
)

DB_PATH = Path("data/memory/memory.db")


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO memory_sources(memory_id, note_id, relation, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (memory_id, note_id, relation, utc_now_iso()),
        )
        return cur.rowcount > 0


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
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            return None
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
    return get_memory(memory_id, db_path=db_path)


def mark_accessed(memory_ids: list[str], db_path: str | Path | None = None) -> None:
    if not memory_ids:
        return
    init_db(db_path)
    now = utc_now_iso()
    with _connect(db_path) as conn:
        conn.executemany(
            "UPDATE memories SET last_accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
            [(now, memory_id) for memory_id in memory_ids],
        )


def soft_delete_memory(memory_id: str, *, reason: str = "user_forget", db_path: str | Path | None = None) -> MemoryRecord | None:
    return update_memory(memory_id, status="deleted", reason=reason, db_path=db_path)


def correct_memory(memory_id: str, content: str, *, reason: str = "user_correct", db_path: str | Path | None = None) -> MemoryRecord | None:
    return update_memory(memory_id, content=content, status="active", reason=reason, db_path=db_path)


def purge_memory(memory_id: str, db_path: str | Path | None = None) -> bool:
    """Permanently remove one memory and its local indexes after a soft delete or explicit user request."""
    init_db(db_path)
    with _connect(db_path) as conn:
        exists = conn.execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if exists is None:
            return False
        conn.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (memory_id,))
        conn.execute("DELETE FROM memory_sources WHERE memory_id = ?", (memory_id,))
        conn.execute("DELETE FROM memory_versions WHERE memory_id = ?", (memory_id,))
        conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return True


def note_has_memory(note_id: str, db_path: str | Path | None = None) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT 1 FROM memory_sources WHERE note_id = ? LIMIT 1", (note_id,)).fetchone()
        return row is not None


def search_memories(
    space_id: str,
    query: str,
    *,
    memory_type: str | None = None,
    include_inactive: bool = False,
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
    scored = [(memory, score) for memory, score in scored if score > 0]
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
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    total = 0
    for row in rows:
        count = int(row["count"])
        total += count
        by_type[str(row["memory_type"])] = by_type.get(str(row["memory_type"]), 0) + count
        by_status[str(row["status"])] = by_status.get(str(row["status"]), 0) + count
    return {"total": total, "by_type": by_type, "by_status": by_status}


def schema_tables(db_path: str | Path | None = None) -> set[str]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}
