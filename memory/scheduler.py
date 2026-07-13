"""Scheduled Memory V2 consolidation entry points."""

from __future__ import annotations

import logging
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

from core.file_lock import safe_space_id
from memory.consolidator import generate_stable_semantic, merge_duplicate_episodic, process_unextracted_notes
from storage.note_storage import NOTES_DIR

LOGGER = logging.getLogger(__name__)
DEFAULT_MEMORY_SCHEDULER_INTERVAL_SECONDS = 3600


def list_memory_space_ids(notes_dir: Path | None = None) -> list[str]:
    root = notes_dir or NOTES_DIR
    if not root.exists():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir())


def run_memory_consolidation(space_id: str, cadence: str) -> dict[str, Any]:
    cadence = cadence.strip().lower()
    if cadence == "daily":
        return process_unextracted_notes(space_id)
    if cadence == "weekly":
        return merge_duplicate_episodic(space_id)
    if cadence == "monthly":
        return generate_stable_semantic(space_id)
    raise ValueError(f"unknown memory consolidation cadence: {cadence}")


def run_memory_consolidation_once(cadence: str, *, space_ids: list[str] | None = None) -> dict[str, Any]:
    targets = space_ids or list_memory_space_ids()
    results = []
    for space_id in targets:
        try:
            results.append(run_memory_consolidation(safe_space_id(space_id), cadence))
        except Exception as exc:
            LOGGER.exception("Memory consolidation failed: space_id=%s cadence=%s", space_id, cadence)
            results.append({"space_id": space_id, "cadence": cadence, "error": str(exc)})
    return {"cadence": cadence, "space_count": len(targets), "results": results}


def due_cadences(today: date, last_run_dates: dict[str, str]) -> list[str]:
    due = []
    today_key = today.isoformat()
    if last_run_dates.get("daily") != today_key:
        due.append("daily")
    if today.weekday() == 0 and last_run_dates.get("weekly") != today_key:
        due.append("weekly")
    if today.day == 1 and last_run_dates.get("monthly") != today_key:
        due.append("monthly")
    return due


def run_memory_scheduler_tick(last_run_dates: dict[str, str] | None = None, *, today: date | None = None) -> dict[str, Any]:
    state = last_run_dates if last_run_dates is not None else {}
    current_day = today or date.today()
    reports = []
    for cadence in due_cadences(current_day, state):
        report = run_memory_consolidation_once(cadence)
        state[cadence] = current_day.isoformat()
        reports.append(report)
    return {"date": current_day.isoformat(), "ran": [report["cadence"] for report in reports], "reports": reports}


def start_memory_scheduler(interval_seconds: int = DEFAULT_MEMORY_SCHEDULER_INTERVAL_SECONDS) -> threading.Thread:
    last_run_dates: dict[str, str] = {}

    def _loop() -> None:
        while True:
            try:
                run_memory_scheduler_tick(last_run_dates)
            except Exception:
                LOGGER.exception("Memory scheduler tick failed")
            time.sleep(max(60, int(interval_seconds)))

    thread = threading.Thread(target=_loop, name="memory-scheduler", daemon=True)
    thread.start()
    return thread
