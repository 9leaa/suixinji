"""Idempotently migrate local Suixinji stores into PostgreSQL."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, inspect, select
from sqlalchemy.dialects.postgresql import insert

from infrastructure.database import get_engine, session_scope
from infrastructure.schema import (
    Delivery,
    InboxMessage,
    Memory,
    MemoryConsolidationRun,
    MemoryDecision,
    MemoryExtractionState,
    MemoryRelation,
    MemorySource,
    MemoryTrace,
    MemoryVector,
    MemoryVersion,
    Note,
    NoteEmbedding,
    NoteRelation,
    NoteTag,
    SummarySubscriptionRow,
    Tenant,
)
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space, parse_datetime

MIGRATED_MODELS = (
    InboxMessage,
    Note,
    NoteTag,
    NoteRelation,
    NoteEmbedding,
    Memory,
    MemorySource,
    MemoryVersion,
    MemoryVector,
    MemoryExtractionState,
    MemoryConsolidationRun,
    MemoryDecision,
    MemoryRelation,
    MemoryTrace,
    SummarySubscriptionRow,
    Delivery,
)


def _json(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            value = line.strip()
            if not value:
                continue
            try:
                yield json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: {exc}") from exc


def _collect(data_dir: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    failures: list[dict[str, str]] = []
    payload: dict[str, Any] = {
        "wal": [], "notes": [], "vectors": [], "memory": {}, "deliveries": {}, "subscriptions": {},
    }
    for path in sorted((data_dir / "cache").glob("*.jsonl")):
        try:
            payload["wal"].extend(_iter_jsonl(path))
        except Exception as exc:
            failures.append({"source": str(path), "error": f"{type(exc).__name__}: {exc}"})
    for index_path in sorted((data_dir / "notes").glob("*/index.json")):
        try:
            payload["notes"].extend(_read_json(index_path, []))
        except Exception as exc:
            failures.append({"source": str(index_path), "error": f"{type(exc).__name__}: {exc}"})
    for vector_path in sorted((data_dir / "notes").glob("*/vectors/index.json")):
        try:
            space_id = vector_path.parents[1].name
            payload["vectors"].extend({"space_id": space_id, **item} for item in _read_json(vector_path, []))
        except Exception as exc:
            failures.append({"source": str(vector_path), "error": f"{type(exc).__name__}: {exc}"})
    memory_path = data_dir / "memory" / "memory.db"
    if memory_path.exists():
        try:
            with sqlite3.connect(memory_path) as conn:
                conn.row_factory = sqlite3.Row
                tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in (
                    "memories", "memory_sources", "memory_versions", "memory_vectors",
                    "memory_extraction_states", "memory_consolidation_runs", "memory_decisions",
                    "memory_relations", "memory_traces",
                ):
                    payload["memory"][table] = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")] if table in tables else []
        except Exception as exc:
            failures.append({"source": str(memory_path), "error": f"{type(exc).__name__}: {exc}"})
    for key, path in (
        ("deliveries", data_dir / "deliveries" / "index.json"),
        ("subscriptions", data_dir / "summary_subscriptions.json"),
    ):
        try:
            payload[key] = _read_json(path, {})
        except Exception as exc:
            failures.append({"source": str(path), "error": f"{type(exc).__name__}: {exc}"})
    return payload, failures


def _local_counts(payload: dict[str, Any]) -> dict[str, int]:
    unique_messages = {
        (str(item.get("source") or "feishu"), str(item.get("message_id") or ""))
        for item in payload["wal"]
        if item.get("message_id")
    }
    counts = {
        "inbox_messages": len(unique_messages),
        "inbox_messages_raw": len(payload["wal"]),
        "notes": len(payload["notes"]),
        "note_embeddings": len(payload["vectors"]),
        "deliveries": len(payload["deliveries"]),
        "summary_subscriptions": len(payload["subscriptions"]),
    }
    counts.update({name: len(rows) for name, rows in payload["memory"].items()})
    return counts


def _database_counts() -> dict[str, int]:
    existing = set(inspect(get_engine()).get_table_names())
    counts: dict[str, int] = {}
    with session_scope() as session:
        for model in MIGRATED_MODELS:
            name = model.__tablename__
            if name in existing:
                counts[name] = int(session.execute(select(func.count()).select_from(model)).scalar_one())
    return counts


def _space_ids(payload: dict[str, Any]) -> set[str]:
    ids = {str(item.get("space_id") or "") for item in payload["wal"] + payload["notes"] + payload["vectors"]}
    for table in ("memories", "memory_extraction_states", "memory_consolidation_runs", "memory_decisions", "memory_relations", "memory_traces"):
        ids.update(str(item.get("space_id") or "") for item in payload["memory"].get(table, []))
    ids.update(str(item.get("space_id") or key) for key, item in payload["deliveries"].items())
    ids.update(str(item.get("space_id") or key) for key, item in payload["subscriptions"].items())
    return {space_id for space_id in ids if space_id}


def _insert_once(session: Any, model: Any, values: dict[str, Any]) -> int:
    with session.begin_nested():
        primary_key = list(model.__table__.primary_key.columns)
        result = session.execute(insert(model).values(**values).on_conflict_do_nothing().returning(*primary_key))
        return 1 if result.first() is not None else 0


def _migrate(payload: dict[str, Any], failures: list[dict[str, str]]) -> dict[str, int]:
    inserted: Counter[str] = Counter()
    with session_scope() as session:
        session.execute(insert(Tenant).values(id=DEFAULT_TENANT_ID, name=DEFAULT_TENANT_ID).on_conflict_do_nothing())
        for space_id in sorted(_space_ids(payload)):
            ensure_tenant_space(session, space_id)

        sequence_by_space: Counter[str] = Counter()
        for item in payload["wal"]:
            try:
                space_id = str(item["space_id"])
                sequence_by_space[space_id] += 1
                inserted["inbox_messages"] += _insert_once(session, InboxMessage, {
                    "id": str(item["id"]),
                    "source": str(item.get("source") or "feishu"),
                    "source_message_id": str(item["message_id"]),
                    "source_event_id": item.get("event_id"),
                    "tenant_id": DEFAULT_TENANT_ID,
                    "space_id": space_id,
                    "chat_id": item.get("chat_id"),
                    "chat_type": item.get("chat_type"),
                    "sender_json": dict(item.get("sender") or {}),
                    "text": str(item.get("text") or ""),
                    "received_at": parse_datetime(item.get("ts")),
                    "status": str(item.get("status") or "pending"),
                    "sensitivity": str(item.get("sensitivity") or "normal"),
                    "sequence_no": sequence_by_space[space_id],
                })
            except Exception as exc:
                failures.append({"source": f"wal:{item.get('id')}", "error": f"{type(exc).__name__}: {exc}"})

        for item in payload["notes"]:
            try:
                space_id = str(item["space_id"])
                note_id = str(item["id"])
                standard = {
                    "id", "message_id", "space_id", "ts", "title", "tags", "type", "summary", "text", "related",
                    "enrichment_status", "enrichment_attempts", "enrichment_error", "enrichment_started_at",
                    "enrichment_updated_at", "sensitivity",
                }
                inserted["notes"] += _insert_once(session, Note, {
                    "id": note_id,
                    "message_id": str(item["message_id"]),
                    "tenant_id": DEFAULT_TENANT_ID,
                    "space_id": space_id,
                    "created_at": parse_datetime(item.get("ts")),
                    "title": str(item.get("title") or ""),
                    "note_type": str(item.get("type") or "other"),
                    "summary": str(item.get("summary") or ""),
                    "text": str(item.get("text") or ""),
                    "metadata_json": {key: value for key, value in item.items() if key not in standard},
                    "enrichment_status": str(item.get("enrichment_status") or "ready"),
                    "enrichment_attempts": int(item.get("enrichment_attempts") or 0),
                    "enrichment_error": item.get("enrichment_error"),
                    "enrichment_started_at": parse_datetime(item["enrichment_started_at"]) if item.get("enrichment_started_at") else None,
                    "enrichment_updated_at": parse_datetime(item["enrichment_updated_at"]) if item.get("enrichment_updated_at") else None,
                    "sensitivity": str(item.get("sensitivity") or "normal"),
                })
                for tag in item.get("tags") or []:
                    inserted["note_tags"] += _insert_once(session, NoteTag, {"note_id": note_id, "tag": str(tag)})
                for target in item.get("related") or []:
                    inserted["note_relations"] += _insert_once(session, NoteRelation, {
                        "source_note_id": note_id, "target_note_id": str(target), "relation": "related",
                    })
            except Exception as exc:
                failures.append({"source": f"note:{item.get('id')}", "error": f"{type(exc).__name__}: {exc}"})

        for item in payload["vectors"]:
            try:
                embedding = [float(value) for value in item.get("embedding") or []]
                if len(embedding) != 1024:
                    raise ValueError(f"embedding dimension is {len(embedding)}, expected 1024")
                metadata = dict(item.get("metadata") or {})
                metadata.setdefault("message_id", item.get("message_id"))
                inserted["note_embeddings"] += _insert_once(session, NoteEmbedding, {
                    "note_id": str(item["note_id"]),
                    "model": str(metadata.get("embedding_model") or "legacy"),
                    "dimensions": len(embedding),
                    "embedding": embedding,
                    "text": str(item.get("text") or ""),
                    "metadata_json": metadata,
                })
            except Exception as exc:
                failures.append({"source": f"vector:{item.get('note_id')}", "error": f"{type(exc).__name__}: {exc}"})

        model_mapping = (
            ("memories", Memory),
            ("memory_sources", MemorySource),
            ("memory_versions", MemoryVersion),
            ("memory_extraction_states", MemoryExtractionState),
            ("memory_relations", MemoryRelation),
        )
        for table, model in model_mapping:
            columns = set(model.__table__.columns.keys())
            for item in payload["memory"].get(table, []):
                try:
                    values = {key: value for key, value in item.items() if key in columns}
                    if table == "memories":
                        values["tenant_id"] = DEFAULT_TENANT_ID
                    inserted[table] += _insert_once(session, model, values)
                except Exception as exc:
                    failures.append({"source": f"{table}:{item.get('id') or item.get('memory_id')}", "error": f"{type(exc).__name__}: {exc}"})

        for item in payload["memory"].get("memory_vectors", []):
            try:
                embedding = _json(item.get("embedding_json"), None)
                if embedding is not None and len(embedding) != 1024:
                    raise ValueError(f"embedding dimension is {len(embedding)}, expected 1024")
                inserted["memory_vectors"] += _insert_once(session, MemoryVector, {
                    "memory_id": item["memory_id"], "embedding": embedding, "model": item.get("model"),
                    "created_at": item["created_at"], "updated_at": item["updated_at"],
                })
            except Exception as exc:
                failures.append({"source": f"memory_vectors:{item.get('memory_id')}", "error": f"{type(exc).__name__}: {exc}"})

        for item in payload["memory"].get("memory_consolidation_runs", []):
            values = dict(item)
            values["result_json"] = _json(values.get("result_json"), None)
            try:
                inserted["memory_consolidation_runs"] += _insert_once(session, MemoryConsolidationRun, values)
            except Exception as exc:
                failures.append({"source": f"memory_consolidation_runs:{item.get('id')}", "error": f"{type(exc).__name__}: {exc}"})

        for item in payload["memory"].get("memory_decisions", []):
            values = dict(item)
            for key in ("target_memory_ids_json", "evidence_json", "result_memory_ids_json"):
                values[key] = _json(values.get(key), [] if key != "result_memory_ids_json" else None)
            try:
                inserted["memory_decisions"] += _insert_once(session, MemoryDecision, values)
            except Exception as exc:
                failures.append({"source": f"memory_decisions:{item.get('id')}", "error": f"{type(exc).__name__}: {exc}"})

        for item in payload["memory"].get("memory_traces", []):
            values = dict(item)
            values["payload_json"] = _json(values.get("payload_json"), {})
            try:
                inserted["memory_traces"] += _insert_once(session, MemoryTrace, values)
            except Exception as exc:
                failures.append({"source": f"memory_traces:{item.get('trace_id')}", "error": f"{type(exc).__name__}: {exc}"})

        for key, item in payload["subscriptions"].items():
            try:
                inserted["summary_subscriptions"] += _insert_once(session, SummarySubscriptionRow, {
                    "space_id": str(item.get("space_id") or key),
                    "tenant_id": DEFAULT_TENANT_ID,
                    "chat_id": str(item.get("chat_id") or ""),
                    "enabled": bool(item.get("enabled", True)),
                    "time": str(item.get("time") or "22:00"),
                    "range_key": str(item.get("range_key") or "today"),
                    "last_sent_date": item.get("last_sent_date"),
                })
            except Exception as exc:
                failures.append({"source": f"summary_subscription:{key}", "error": f"{type(exc).__name__}: {exc}"})

        for key, item in payload["deliveries"].items():
            try:
                inserted["deliveries"] += _insert_once(session, Delivery, {
                    "delivery_key": str(item.get("delivery_key") or key),
                    "delivery_type": str(item.get("delivery_type") or "unknown"),
                    "tenant_id": DEFAULT_TENANT_ID,
                    "space_id": str(item.get("space_id") or "unknown"),
                    "message_id": item.get("message_id"),
                    "status": str(item.get("status") or "unknown"),
                    "created_at": str(item.get("created_at") or datetime.now().astimezone().isoformat()),
                    "updated_at": str(item.get("updated_at") or datetime.now().astimezone().isoformat()),
                    "reserved_at": item.get("reserved_at"),
                    "lease_expires_at": item.get("lease_expires_at"),
                    "attempt_count": int(item.get("attempt_count") or 0),
                    "error": item.get("error"),
                })
            except Exception as exc:
                failures.append({"source": f"delivery:{key}", "error": f"{type(exc).__name__}: {exc}"})
    return dict(inserted)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--failure-output", type=Path, default=Path("data/migration_failures.json"))
    args = parser.parse_args()

    payload, failures = _collect(args.data_dir)
    expected_tables = {model.__tablename__ for model in MIGRATED_MODELS}
    missing = expected_tables - set(inspect(get_engine()).get_table_names())
    if missing:
        raise SystemExit("PostgreSQL schema is missing tables; run `alembic upgrade head`: " + ", ".join(sorted(missing)))
    before = _database_counts()
    inserted = {} if args.dry_run else _migrate(payload, failures)
    after = _database_counts()
    report = {
        "mode": "dry-run" if args.dry_run else "migrate",
        "local_counts": _local_counts(payload),
        "postgres_before": before,
        "inserted": inserted,
        "postgres_after": after,
        "failure_count": len(failures),
        "failure_output": str(args.failure_output),
    }
    if failures:
        args.failure_output.parent.mkdir(parents=True, exist_ok=True)
        args.failure_output.write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
