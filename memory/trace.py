"""Privacy-aware JSONL traces for memory writes and reads."""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

from core.sensitive import assess_sensitive_text, redact_sensitive_text
from core.settings import STORAGE_BACKEND
from memory.models import new_id, utc_now_iso

TRACE_PATH = Path("data/memory/traces.jsonl")
LOGGER = logging.getLogger(__name__)
_TRACE_LOCK = threading.RLock()


def _safe_error(error: str | None) -> str | None:
    if not error:
        return None
    value = str(error)
    if assess_sensitive_text(value).blocks_storage:
        return "[sensitive content redacted]"
    value = redact_sensitive_text(value)
    value = re.sub(r"(?:text|output)_preview=('[^']*'|\"[^\"]*\")", "preview=<redacted>", value, flags=re.IGNORECASE)
    value = re.sub(r"(?i)(password|token|api[_ -]?key|secret)\s*[:=]\s*\S+", r"\1=<redacted>", value)
    return value[:500]


def _read_traces(path: str | Path | None = None) -> list[dict[str, Any]]:
    if path is None and STORAGE_BACKEND == "postgres":
        from repositories.postgres.memory import list_memory_traces

        return list_memory_traces()
    trace_path = Path(path or TRACE_PATH)
    with _TRACE_LOCK:
        if not trace_path.exists():
            return []
        items: list[dict[str, Any]] = []
        with trace_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    items.append(json.loads(line))
    return items


def start_trace(trace_type: str, space_id: str, *, note_id: str | None = None, query_len: int | None = None) -> dict[str, Any]:
    trace: dict[str, Any] = {
        "trace_id": new_id("trace"),
        "trace_type": trace_type,
        "space_id": space_id,
        "note_id": note_id,
        "query_len": query_len,
        "started_at": utc_now_iso(),
        "finished_at": None,
        "steps": [],
    }
    return trace


def add_step(
    trace: dict[str, Any] | None,
    step: str,
    *,
    status: str = "success",
    duration_ms: int = 0,
    input_summary: dict[str, Any] | None = None,
    output_summary: dict[str, Any] | None = None,
    reason: str | None = None,
    error: str | None = None,
) -> None:
    if trace is None:
        return
    trace.setdefault("steps", []).append(
        {
            "step": step,
            "status": status,
            "duration_ms": duration_ms,
            "at": utc_now_iso(),
            "input_summary": input_summary or {},
            "output_summary": output_summary or {},
            "reason": reason,
            "error": _safe_error(error),
        }
    )


def finish_trace(trace: dict[str, Any] | None, *, status: str = "success", path: str | Path | None = None) -> dict[str, Any] | None:
    if trace is None:
        return None
    trace["finished_at"] = utc_now_iso()
    trace["status"] = status
    add_step(trace, "trace_finished", status=status)
    if path is not None or STORAGE_BACKEND == "local":
        trace_path = Path(path or TRACE_PATH)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        with _TRACE_LOCK:
            with trace_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(trace, ensure_ascii=False) + "\n")
    try:
        from memory.repository import save_memory_trace

        save_memory_trace(trace)
    except Exception as exc:
        LOGGER.warning("memory.trace.db_persist_failed trace_id=%s error_type=%s", trace.get("trace_id"), type(exc).__name__)
    return trace


def latest_trace(path: str | Path | None = None) -> dict[str, Any] | None:
    traces = _read_traces(path)
    return traces[-1] if traces else None


def get_trace(trace_id: str, path: str | Path | None = None) -> dict[str, Any] | None:
    for item in reversed(_read_traces(path)):
        if item.get("trace_id") == trace_id:
            return item
    return None


def find_traces_by_memory(memory_id: str, path: str | Path | None = None) -> list[dict[str, Any]]:
    matched = []
    for item in _read_traces(path):
        for step in item.get("steps", []):
            output = step.get("output_summary") or {}
            if output.get("memory_id") == memory_id or output.get("target_memory_id") == memory_id:
                matched.append(item)
                break
    return matched
