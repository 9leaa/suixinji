"""One-off, idempotent repair for the 2026-07-15 production validation data.

The runtime policy is deliberately generic.  This migration only repairs the
two already-corrupted preference decisions and removes the sensitive dummy note
created during validation.  It never prints the sensitive note text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.models import normalize_content


DB_PATH = Path("data/memory/memory.db")
NOTES_DIR = Path("data/notes")
CACHE_DIR = Path("data/cache")

LATTE_MEMORY_ID = "mem_a0ac1f0093be"
NEGATIVE_APPLE_MEMORY_ID = "mem_bb3b5e6631ce"
POSITIVE_APPLE_NOTE_ID = "0a0886d7-fc79-4c76-abaf-2a3c6a077bc3"
NEGATIVE_APPLE_NOTE_ID = "96426a4d-7a4b-4a44-9af6-cac95105c015"
SENSITIVE_NOTE_ID = "6e4243fc-80b9-472f-8728-f3570ad7c384"
FALSE_MERGE_DECISION_ID = "decision_b4afbfc60e08"
APPLE_SUPERSEDE_DECISION_ID = "decision_fb2eadf6d0fb"

LATTE_CONTENT = "用户喜欢喝燕麦拿铁，通常选大杯"
POSITIVE_APPLE_CONTENT = "用户喜欢吃苹果"
POSITIVE_AT = "2026-07-15T21:16:18+08:00"
NEGATIVE_AT = "2026-07-15T21:16:29+08:00"


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


POSITIVE_APPLE_MEMORY_ID = _stable_id("mem", f"repair:{POSITIVE_APPLE_NOTE_ID}")


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.repair.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _repair_memories() -> dict[str, int]:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    counts = {"memory_rows": 0, "decision_rows": 0, "relation_rows": 0}
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        latte = conn.execute("SELECT * FROM memories WHERE id = ?", (LATTE_MEMORY_ID,)).fetchone()
        negative = conn.execute("SELECT * FROM memories WHERE id = ?", (NEGATIVE_APPLE_MEMORY_ID,)).fetchone()
        if latte is None or negative is None:
            raise RuntimeError("Expected validation memories are missing; refusing partial repair")
        space_id = str(latte["space_id"])
        if str(negative["space_id"]) != space_id:
            raise RuntimeError("Validation memories belong to different spaces")

        conn.execute(
            """
            UPDATE memories
               SET content = ?, normalized_content = ?, status = 'active', subject = '用户',
                   predicate = 'preference', object_value = '燕麦拿铁', valid_until = NULL,
                   updated_at = ?, last_confirmed_at = ?, current_version = 5
             WHERE id = ?
            """,
            (
                LATTE_CONTENT,
                normalize_content(LATTE_CONTENT),
                now,
                "2026-07-15T21:13:26+08:00",
                LATTE_MEMORY_ID,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO memory_versions(
                id, memory_id, version, content, status, task_status, confidence, importance,
                valid_from, valid_until, reason, source_note_id, created_at
            ) VALUES (?, ?, 5, ?, 'active', NULL, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                _stable_id("ver", f"repair:{LATTE_MEMORY_ID}:5"),
                LATTE_MEMORY_ID,
                LATTE_CONTENT,
                float(latte["confidence"]),
                float(latte["importance"]),
                latte["valid_from"],
                "corrected_false_preference_topic_merge",
                None,
                now,
            ),
        )
        conn.execute(
            "DELETE FROM memory_sources WHERE memory_id = ? AND note_id IN (?, ?)",
            (LATTE_MEMORY_ID, POSITIVE_APPLE_NOTE_ID, NEGATIVE_APPLE_NOTE_ID),
        )
        conn.execute(
            "UPDATE memories SET subject = '用户', predicate = 'preference', object_value = '苹果' WHERE id = ?",
            (NEGATIVE_APPLE_MEMORY_ID,),
        )

        conn.execute(
            """
            INSERT INTO memories(
                id, space_id, memory_type, content, normalized_content, importance, confidence,
                status, task_status, subject, predicate, object_value, valid_from, valid_until,
                last_confirmed_at, created_at, updated_at, last_accessed_at, access_count, current_version
            ) VALUES (?, ?, 'preference', ?, ?, 0.75, 0.86, 'superseded', NULL,
                      '用户', 'preference', '苹果', ?, ?, ?, ?, ?, NULL, 0, 2)
            ON CONFLICT(id) DO UPDATE SET
                content = excluded.content,
                normalized_content = excluded.normalized_content,
                status = excluded.status,
                subject = excluded.subject,
                predicate = excluded.predicate,
                object_value = excluded.object_value,
                valid_from = excluded.valid_from,
                valid_until = excluded.valid_until,
                last_confirmed_at = excluded.last_confirmed_at,
                updated_at = excluded.updated_at,
                current_version = 2
            """,
            (
                POSITIVE_APPLE_MEMORY_ID,
                space_id,
                POSITIVE_APPLE_CONTENT,
                normalize_content(POSITIVE_APPLE_CONTENT),
                POSITIVE_AT,
                NEGATIVE_AT,
                NEGATIVE_AT,
                POSITIVE_AT,
                NEGATIVE_AT,
            ),
        )
        versions = (
            (1, "active", None, "created_after_topic_repair", POSITIVE_APPLE_NOTE_ID, POSITIVE_AT),
            (2, "superseded", NEGATIVE_AT, "explicit_preference_change", NEGATIVE_APPLE_NOTE_ID, NEGATIVE_AT),
        )
        for version, status, valid_until, reason, source_note_id, created_at in versions:
            conn.execute(
                """
                INSERT OR IGNORE INTO memory_versions(
                    id, memory_id, version, content, status, task_status, confidence, importance,
                    valid_from, valid_until, reason, source_note_id, created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, 0.86, 0.75, ?, ?, ?, ?, ?)
                """,
                (
                    _stable_id("ver", f"repair:{POSITIVE_APPLE_MEMORY_ID}:{version}"),
                    POSITIVE_APPLE_MEMORY_ID,
                    version,
                    POSITIVE_APPLE_CONTENT,
                    status,
                    POSITIVE_AT,
                    valid_until,
                    reason,
                    source_note_id,
                    created_at,
                ),
            )
        conn.execute(
            "DELETE FROM memory_sources WHERE memory_id = ?",
            (POSITIVE_APPLE_MEMORY_ID,),
        )
        conn.executemany(
            "INSERT INTO memory_sources(memory_id, note_id, relation, created_at) VALUES (?, ?, ?, ?)",
            (
                (POSITIVE_APPLE_MEMORY_ID, POSITIVE_APPLE_NOTE_ID, "created_from", POSITIVE_AT),
                (POSITIVE_APPLE_MEMORY_ID, NEGATIVE_APPLE_NOTE_ID, "contradicted_by", NEGATIVE_AT),
            ),
        )

        conn.execute(
            """
            DELETE FROM memory_relations
             WHERE (source_memory_id = ? AND target_memory_id = ?)
                OR (source_memory_id = ? AND target_memory_id = ?)
                OR source_memory_id = ? OR target_memory_id = ?
            """,
            (
                NEGATIVE_APPLE_MEMORY_ID,
                LATTE_MEMORY_ID,
                LATTE_MEMORY_ID,
                NEGATIVE_APPLE_MEMORY_ID,
                POSITIVE_APPLE_MEMORY_ID,
                POSITIVE_APPLE_MEMORY_ID,
            ),
        )
        relations = (
            (NEGATIVE_APPLE_MEMORY_ID, POSITIVE_APPLE_MEMORY_ID, "supersedes"),
            (POSITIVE_APPLE_MEMORY_ID, NEGATIVE_APPLE_MEMORY_ID, "superseded_by"),
        )
        for source_id, target_id, relation in relations:
            conn.execute(
                """
                INSERT INTO memory_relations(
                    id, space_id, source_memory_id, target_memory_id, relation, decision_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _stable_id("rel", f"repair:{source_id}:{target_id}:{relation}"),
                    space_id,
                    source_id,
                    target_id,
                    relation,
                    APPLE_SUPERSEDE_DECISION_ID,
                    NEGATIVE_AT,
                ),
            )
        counts["relation_rows"] = len(relations)

        false_merge_reason = "corrected_false_topic_merge; original_relation=merge; original_reason=compatible_extension"
        conn.execute(
            """
            UPDATE memory_decisions
               SET relation = 'new', target_memory_ids_json = '[]', confidence = 0.86,
                   reason = ?, evidence_json = ?, recommended_action = 'insert', status = 'applied',
                   result_memory_ids_json = ?, error = NULL
             WHERE id = ?
            """,
            (
                false_merge_reason,
                json.dumps([f"note:{POSITIVE_APPLE_NOTE_ID}"], ensure_ascii=False),
                json.dumps([POSITIVE_APPLE_MEMORY_ID], ensure_ascii=False),
                FALSE_MERGE_DECISION_ID,
            ),
        )
        conn.execute(
            """
            UPDATE memory_decisions
               SET target_memory_ids_json = ?, reason = ?, evidence_json = ?,
                   result_memory_ids_json = ?, status = 'applied', error = NULL
             WHERE id = ?
            """,
            (
                json.dumps([POSITIVE_APPLE_MEMORY_ID], ensure_ascii=False),
                "explicit_preference_change; corrected_target_after_topic_repair",
                json.dumps(
                    [f"note:{NEGATIVE_APPLE_NOTE_ID}", f"memory:{POSITIVE_APPLE_MEMORY_ID}"],
                    ensure_ascii=False,
                ),
                json.dumps([NEGATIVE_APPLE_MEMORY_ID, POSITIVE_APPLE_MEMORY_ID], ensure_ascii=False),
                APPLE_SUPERSEDE_DECISION_ID,
            ),
        )
        counts["decision_rows"] = 2
        conn.execute(
            "DELETE FROM memory_vectors WHERE memory_id IN (?, ?, ?)",
            (LATTE_MEMORY_ID, NEGATIVE_APPLE_MEMORY_ID, POSITIVE_APPLE_MEMORY_ID),
        )
        counts["memory_rows"] = 3
    return counts


def _remove_sensitive_note() -> dict[str, int]:
    counts = {"index_rows": 0, "markdown_blocks": 0, "vector_rows": 0, "wal_rows": 0, "trace_rows": 0}
    secret_text = ""

    if NOTES_DIR.exists():
        for index_path in NOTES_DIR.glob("*/index.json"):
            items = json.loads(index_path.read_text(encoding="utf-8"))
            changed = False
            kept = []
            for item in items:
                if str(item.get("id") or "") == SENSITIVE_NOTE_ID:
                    secret_text = str(item.get("text") or secret_text)
                    counts["index_rows"] += 1
                    changed = True
                    continue
                related = list(item.get("related") or [])
                if SENSITIVE_NOTE_ID in related:
                    item["related"] = [note_id for note_id in related if note_id != SENSITIVE_NOTE_ID]
                    changed = True
                kept.append(item)
            if changed:
                _write_json(index_path, kept)

        marker = f"- id: `{SENSITIVE_NOTE_ID}`"
        for markdown_path in NOTES_DIR.glob("*/*.md"):
            content = markdown_path.read_text(encoding="utf-8")
            if marker not in content:
                continue
            parts = re.split(r"(?=^## )", content, flags=re.MULTILINE)
            kept_parts = [part for part in parts if marker not in part]
            markdown_path.write_text("".join(kept_parts), encoding="utf-8")
            counts["markdown_blocks"] += len(parts) - len(kept_parts)

        for vector_path in NOTES_DIR.glob("*/vectors/index.json"):
            items = json.loads(vector_path.read_text(encoding="utf-8"))
            kept = []
            for item in items:
                if str(item.get("note_id") or "") == SENSITIVE_NOTE_ID:
                    secret_text = str(item.get("text") or secret_text)
                    counts["vector_rows"] += 1
                    continue
                kept.append(item)
            if len(kept) != len(items):
                _write_json(vector_path, kept)

    if CACHE_DIR.exists():
        for wal_path in CACHE_DIR.glob("*.jsonl"):
            lines = []
            changed = False
            for raw_line in wal_path.read_text(encoding="utf-8").splitlines():
                if not raw_line.strip():
                    continue
                record = json.loads(raw_line)
                if str(record.get("id") or "") == SENSITIVE_NOTE_ID:
                    secret_text = str(record.get("text") or secret_text)
                    record["text"] = "[敏感内容已拦截，原文未保存]"
                    record["status"] = "blocked_sensitive"
                    record["sensitivity"] = "credential"
                    counts["wal_rows"] += 1
                    changed = True
                lines.append(json.dumps(record, ensure_ascii=False))
            if changed:
                wal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM memory_sources WHERE note_id = ?", (SENSITIVE_NOTE_ID,))
        conn.execute("DELETE FROM memory_decisions WHERE note_id = ?", (SENSITIVE_NOTE_ID,))
        conn.execute("DELETE FROM memory_extraction_states WHERE note_id = ?", (SENSITIVE_NOTE_ID,))
        cursor = conn.execute("DELETE FROM memory_traces WHERE note_id = ?", (SENSITIVE_NOTE_ID,))
        counts["trace_rows"] += max(0, int(cursor.rowcount))

    trace_path = Path("data/memory/traces.jsonl")
    if trace_path.exists():
        kept_lines = []
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if str(payload.get("note_id") or "") == SENSITIVE_NOTE_ID:
                counts["trace_rows"] += 1
                continue
            kept_lines.append(json.dumps(payload, ensure_ascii=False))
        trace_path.write_text("\n".join(kept_lines) + ("\n" if kept_lines else ""), encoding="utf-8")

    if secret_text:
        for log_path in list(Path("data/logs").glob("*.log")) + list(Path("data/logs").glob("*.jsonl")):
            content = log_path.read_text(encoding="utf-8", errors="replace")
            if secret_text in content:
                log_path.write_text(content.replace(secret_text, "[sensitive content redacted]"), encoding="utf-8")

    return counts


def _verify() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        latte = conn.execute("SELECT * FROM memories WHERE id = ?", (LATTE_MEMORY_ID,)).fetchone()
        positive = conn.execute("SELECT * FROM memories WHERE id = ?", (POSITIVE_APPLE_MEMORY_ID,)).fetchone()
        negative = conn.execute("SELECT * FROM memories WHERE id = ?", (NEGATIVE_APPLE_MEMORY_ID,)).fetchone()
        assert latte is not None and latte["status"] == "active" and latte["content"] == LATTE_CONTENT
        assert positive is not None and positive["status"] == "superseded" and positive["object_value"] == "苹果"
        assert negative is not None and negative["status"] == "active" and negative["object_value"] == "苹果"
        bad_sources = conn.execute(
            "SELECT COUNT(*) FROM memory_sources WHERE memory_id = ? AND note_id IN (?, ?)",
            (LATTE_MEMORY_ID, POSITIVE_APPLE_NOTE_ID, NEGATIVE_APPLE_NOTE_ID),
        ).fetchone()[0]
        assert bad_sources == 0

    for index_path in NOTES_DIR.glob("*/index.json"):
        items = json.loads(index_path.read_text(encoding="utf-8"))
        assert all(str(item.get("id") or "") != SENSITIVE_NOTE_ID for item in items)
    for vector_path in NOTES_DIR.glob("*/vectors/index.json"):
        items = json.loads(vector_path.read_text(encoding="utf-8"))
        assert all(str(item.get("note_id") or "") != SENSITIVE_NOTE_ID for item in items)
    for wal_path in CACHE_DIR.glob("*.jsonl"):
        for line in wal_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if str(record.get("id") or "") == SENSITIVE_NOTE_ID:
                assert record.get("status") == "blocked_sensitive"
                assert record.get("text") == "[敏感内容已拦截，原文未保存]"


def main() -> None:
    memory_counts = _repair_memories()
    privacy_counts = _remove_sensitive_note()
    _verify()
    print(json.dumps({"status": "ok", "memory": memory_counts, "privacy": privacy_counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
