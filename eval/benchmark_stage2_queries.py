"""Measure Stage 2 PostgreSQL query paths with bounded, disposable data."""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import delete, text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import query_agent
from infrastructure.database import get_engine, session_scope
from infrastructure.schema import Memory, MemorySource, MemoryVersion, Note, NoteRelation, NoteTag, Tenant
from repositories.postgres import memory as memory_repository
from repositories.postgres.common import ensure_tenant_space
from runtime.query_metrics import capture_sql_queries


NOTE_SIZES = (1_000, 10_000)


def _percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * ratio)))
    return round(ordered[index], 3)


def _chunks(items: list[dict[str, Any]], size: int = 1_000) -> list[list[dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _bulk_insert(session: Any, model: Any, rows: list[dict[str, Any]]) -> None:
    for chunk in _chunks(rows):
        session.execute(model.__table__.insert(), chunk)


def _seed_notes(tenant_id: str, space_id: str, count: int) -> None:
    now = datetime.now().astimezone()
    types = ("学习", "生活", "任务", "资料")
    type_tags = {"学习": "笔记", "生活": "饮食", "任务": "待办", "资料": "文档"}
    notes: list[dict[str, Any]] = []
    tags: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    for index in range(count):
        note_id = f"{space_id}-note-{index:05d}"
        note_type = types[index % len(types)]
        created_at = now - timedelta(seconds=count - index)
        notes.append(
            {
                "id": note_id,
                "message_id": f"{space_id}-message-{index:05d}",
                "tenant_id": tenant_id,
                "space_id": space_id,
                "created_at": created_at,
                "title": f"性能测试笔记 {index}",
                "note_type": note_type,
                "summary": f"第 {index} 条查询性能样本",
                "text": f"这是第 {index} 条查询性能样本，用于验证数据库查询不会全空间扫描。",
                "metadata_json": {},
                "enrichment_status": "provisional" if index % 20 == 0 else "ready",
                "enrichment_attempts": 0,
                "sensitivity": "normal",
            }
        )
        tags.append({"note_id": note_id, "tag": type_tags[note_type]})
        if index and index % 10 == 0:
            relations.append(
                {
                    "source_note_id": note_id,
                    "target_note_id": f"{space_id}-note-{index - 1:05d}",
                    "relation": "related",
                    "created_at": created_at,
                }
            )
    with session_scope() as session:
        ensure_tenant_space(session, space_id, tenant_id=tenant_id, source="benchmark")
        _bulk_insert(session, Note, notes)
        _bulk_insert(session, NoteTag, tags)
        _bulk_insert(session, NoteRelation, relations)


def _seed_memories(tenant_id: str, space_id: str, count: int = 100) -> None:
    now = datetime.now().astimezone().isoformat()
    memories: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    versions: list[dict[str, Any]] = []
    for index in range(count):
        memory_id = f"{space_id}-memory-{index:03d}"
        content = f"用户正在学习数据库性能主题 {index}。"
        memories.append(
            {
                "id": memory_id,
                "tenant_id": tenant_id,
                "space_id": space_id,
                "memory_type": "semantic",
                "content": content,
                "normalized_content": content.casefold(),
                "importance": 0.7,
                "confidence": 0.9,
                "status": "active",
                "subject": "user",
                "predicate": "learning_focus",
                "object_value": f"数据库性能主题 {index}",
                "memory_key": f"semantic:user:learning_focus:{index}",
                "scope_json": {},
                "valid_from": now,
                "last_confirmed_at": now,
                "created_at": now,
                "updated_at": now,
                "access_count": 0,
                "current_version": 1,
            }
        )
        sources.append(
            {
                "memory_id": memory_id,
                "note_id": f"{space_id}-source-{index:03d}",
                "relation": "created_from",
                "created_at": now,
            }
        )
        versions.append(
            {
                "id": f"{space_id}-version-{index:03d}",
                "memory_id": memory_id,
                "version": 1,
                "content": content,
                "status": "active",
                "confidence": 0.9,
                "importance": 0.7,
                "valid_from": now,
                "reason": "benchmark_seed",
                "source_note_id": f"{space_id}-source-{index:03d}",
                "created_at": now,
            }
        )
    with session_scope() as session:
        ensure_tenant_space(session, space_id, tenant_id=tenant_id, source="benchmark")
        _bulk_insert(session, Memory, memories)
        _bulk_insert(session, MemorySource, sources)
        _bulk_insert(session, MemoryVersion, versions)


def _measure(name: str, operation: Callable[[], Any], repetitions: int) -> dict[str, Any]:
    engine = get_engine()
    durations: list[float] = []
    sql_counts: list[int] = []
    result_sizes: list[int] = []
    for _ in range(repetitions):
        started = time.perf_counter()
        with capture_sql_queries(engine) as stats:
            result = operation()
        durations.append((time.perf_counter() - started) * 1_000)
        sql_counts.append(stats.count)
        result_sizes.append(len(result) if hasattr(result, "__len__") else 1)
    return {
        "name": name,
        "repetitions": repetitions,
        "sql_count": {"min": min(sql_counts), "max": max(sql_counts)},
        "wall_latency_ms": {
            "p50": _percentile(durations, 0.50),
            "p95": _percentile(durations, 0.95),
            "mean": round(statistics.fmean(durations), 3),
        },
        "result_size": {"min": min(result_sizes), "max": max(result_sizes)},
    }


def _explain(statement: str, **parameters: Any) -> dict[str, Any]:
    with get_engine().connect() as connection:
        payload = connection.execute(
            text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}"),
            parameters,
        ).scalar_one()
    return payload[0] if isinstance(payload, list) else payload


def _cleanup(tenant_id: str) -> None:
    with session_scope() as session:
        session.execute(delete(Tenant).where(Tenant.id == tenant_id))


def run(output: Path, *, repetitions: int) -> dict[str, Any]:
    tenant_id = f"stage2-benchmark-{uuid.uuid4().hex}"
    note_spaces = {size: f"{tenant_id}-notes-{size}" for size in NOTE_SIZES}
    memory_space = f"{tenant_id}-memories"
    report: dict[str, Any] = {}
    cleaned = False
    try:
        for size, space_id in note_spaces.items():
            _seed_notes(tenant_id, space_id, size)
        _seed_memories(tenant_id, memory_space)

        measurements: dict[str, Any] = {
            "memory_list_top_100": _measure(
                "memory_list_top_100",
                lambda: memory_repository.list_memories(memory_space, limit=100),
                repetitions,
            )
        }
        for size, space_id in note_spaces.items():
            target_id = f"{space_id}-note-{size - 10:05d}"
            measurements[f"notes_{size}"] = {
                "filter_type": _measure(
                    "filter_type",
                    lambda space_id=space_id: query_agent.filter_notes(space_id, note_type="学习", limit=30),
                    repetitions,
                ),
                "filter_tag": _measure(
                    "filter_tag",
                    lambda space_id=space_id: query_agent.filter_notes(space_id, tags=["笔记"], limit=30),
                    repetitions,
                ),
                "list_recent": _measure(
                    "list_recent",
                    lambda space_id=space_id: query_agent.list_recent(space_id, days=365, limit=30),
                    repetitions,
                ),
                "get_note": _measure(
                    "get_note",
                    lambda space_id=space_id, target_id=target_id: query_agent.get_note(space_id, target_id),
                    repetitions,
                ),
                "follow_links": _measure(
                    "follow_links",
                    lambda space_id=space_id, target_id=target_id: query_agent.follow_links(space_id, target_id, limit=5),
                    repetitions,
                ),
                "provisional_search": _measure(
                    "provisional_search",
                    lambda space_id=space_id: query_agent.provisional_search(space_id, "数据库查询性能", limit=5),
                    repetitions,
                ),
            }

        largest_space = note_spaces[10_000]
        explains = {
            "type_recent": _explain(
                "SELECT id FROM notes WHERE space_id = :space_id AND note_type = '学习' "
                "ORDER BY created_at DESC LIMIT 30",
                space_id=largest_space,
            ),
            "tag_recent": _explain(
                "SELECT n.id FROM notes n JOIN note_tags t ON t.note_id = n.id "
                "WHERE n.space_id = :space_id AND t.tag = '笔记' ORDER BY n.created_at DESC LIMIT 30",
                space_id=largest_space,
            ),
            "provisional_recent": _explain(
                "SELECT id FROM notes WHERE space_id = :space_id "
                "AND enrichment_status IN ('provisional', 'enriching', 'failed') "
                "ORDER BY created_at DESC LIMIT 100",
                space_id=largest_space,
            ),
        }
        report = {
            "schema_version": "stage2-query-benchmark-v1",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "branch": subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip(),
            "dataset": {"note_sizes": list(NOTE_SIZES), "memory_count": 100, "repetitions": repetitions},
            "storage": {
                "postgres": "existing service through DATABASE_URL",
                "docker_controlled_by_benchmark": False,
                "docker_host_used": False,
            },
            "measurements": measurements,
            "explain_analyze": explains,
        }
    finally:
        _cleanup(tenant_id)
        cleaned = True
    report["storage"]["test_data_cleaned"] = cleaned
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    report = run(args.output, repetitions=max(1, min(args.repetitions, 10)))
    print(json.dumps({"output": str(args.output), "measurements": report["measurements"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
