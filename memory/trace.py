"""Privacy-aware JSONL traces for memory writes and reads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memory.models import new_id, utc_now_iso

TRACE_PATH = Path("data/memory/traces.jsonl")


def _read_traces(path: str | Path | None = None) -> list[dict[str, Any]]:
    trace_path = Path(path or TRACE_PATH)
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
            "error": error,
        }
    )


def finish_trace(trace: dict[str, Any] | None, *, status: str = "success", path: str | Path | None = None) -> dict[str, Any] | None:
    if trace is None:
        return None
    trace["finished_at"] = utc_now_iso()
    trace["status"] = status
    add_step(trace, "trace_finished", status=status)
    trace_path = Path(path or TRACE_PATH)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    with trace_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(trace, ensure_ascii=False) + "\n")
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
