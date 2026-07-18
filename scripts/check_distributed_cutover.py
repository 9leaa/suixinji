#!/usr/bin/env python
"""Read-only production cutover checks for the distributed backend."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from dotenv import load_dotenv
from sqlalchemy import inspect, text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from infrastructure.database import get_engine
from infrastructure.redis_client import get_redis
from infrastructure.redis_keys import KEYS
from runtime.streams.client import GROUPS

REQUIRED_TABLES = {
    "inbox_messages",
    "outbox_events",
    "tasks",
    "task_attempts",
    "notes",
    "memories",
    "deliveries",
    "agent_runs",
    "agent_steps",
    "llm_usage",
}


def _add(report: dict[str, Any], level: str, check: str, detail: Any) -> None:
    report[level].append({"check": check, "detail": detail})


def check_configuration(report: dict[str, Any]) -> None:
    expected = {
        "STORAGE_BACKEND": "postgres",
        "COORDINATION_BACKEND": "redis",
        "TASK_QUEUE_BACKEND": "redis_streams",
    }
    for key, value in expected.items():
        actual = os.getenv(key, "").strip().lower()
        if actual != value:
            _add(report, "blockers", key, f"expected {value}, got {actual or '<unset>'}")
        else:
            _add(report, "passed", key, actual)
    if os.getenv("SUIXINJI_FAKE_EXTERNALS", "false").strip().lower() in {"1", "true", "yes", "on"}:
        _add(report, "blockers", "fake_externals", "SUIXINJI_FAKE_EXTERNALS must be false for production")


def check_postgres(report: dict[str, Any], *, allow_pending: bool) -> None:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            tables = set(inspect(conn).get_table_names())
            missing = sorted(REQUIRED_TABLES - tables)
            if missing:
                _add(report, "blockers", "postgres_schema", {"missing_tables": missing})
            else:
                _add(report, "passed", "postgres_schema", "required tables exist")
            pending = int(conn.execute(text("SELECT COUNT(*) FROM tasks WHERE status IN ('queued', 'running', 'retry')")).scalar_one())
            unpublished = int(conn.execute(text("SELECT COUNT(*) FROM outbox_events WHERE published_at IS NULL")).scalar_one())
            dead_letter = int(conn.execute(text("SELECT COUNT(*) FROM tasks WHERE status = 'dead_letter'")).scalar_one())
            if (pending or unpublished) and not allow_pending:
                _add(report, "blockers", "work_backlog", {"pending_tasks": pending, "unpublished_outbox": unpublished})
            else:
                _add(report, "passed", "work_backlog", {"pending_tasks": pending, "unpublished_outbox": unpublished})
            if dead_letter:
                _add(report, "warnings", "dead_letter_tasks", dead_letter)
            else:
                _add(report, "passed", "dead_letter_tasks", 0)
            if "alembic_version" in tables:
                current = str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none() or "")
                config = Config(str(ROOT / "alembic.ini"))
                head = str(ScriptDirectory.from_config(config).get_current_head() or "")
                if current != head:
                    _add(report, "blockers", "alembic_revision", {"current": current, "head": head})
                else:
                    _add(report, "passed", "alembic_revision", current)
    except Exception as exc:
        _add(report, "blockers", "postgres_health", f"{type(exc).__name__}: {exc}")


def check_redis(report: dict[str, Any]) -> None:
    try:
        client = get_redis()
        client.ping()
        group_report = {}
        for task_type, expected_group in GROUPS.items():
            stream = KEYS.stream(task_type)
            try:
                groups = client.xinfo_groups(stream)
            except Exception:
                groups = []
            names = {str(item.get("name")) for item in groups}
            group_report[task_type] = {"expected": expected_group, "present": expected_group in names}
        missing = [task_type for task_type, item in group_report.items() if not item["present"]]
        if missing:
            _add(report, "warnings", "redis_consumer_groups", {"missing_until_first_worker_start": missing})
        else:
            _add(report, "passed", "redis_consumer_groups", group_report)
        _add(report, "passed", "redis_health", "PONG")
    except Exception as exc:
        _add(report, "blockers", "redis_health", f"{type(exc).__name__}: {exc}")


def check_local_recovery_assets(report: dict[str, Any]) -> None:
    cache_files = list((ROOT / "data" / "cache").glob("*.jsonl"))
    note_indexes = list((ROOT / "data" / "notes").glob("*/index.json"))
    backups = list((ROOT / "backups").glob("*")) if (ROOT / "backups").exists() else []
    _add(report, "passed", "local_export_inventory", {"cache_files": len(cache_files), "note_indexes": len(note_indexes)})
    if backups:
        _add(report, "passed", "local_backup", str(max(backups, key=lambda path: path.stat().st_mtime)))
    else:
        _add(report, "warnings", "local_backup", "no backup found; run make backup before the write freeze")
    if not (ROOT / "docs" / "distributed_cutover_runbook.md").exists():
        _add(report, "blockers", "rollback_runbook", "docs/distributed_cutover_runbook.md is missing")
    else:
        _add(report, "passed", "rollback_runbook", "present")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-pending", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report: dict[str, Any] = {"passed": [], "warnings": [], "blockers": []}
    check_configuration(report)
    check_postgres(report, allow_pending=args.allow_pending)
    check_redis(report)
    check_local_recovery_assets(report)
    report["ready"] = not report["blockers"]
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["ready"] else 1)


if __name__ == "__main__":
    main()
