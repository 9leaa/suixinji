"""文件作用：Memory Trace 存储与隐私裁剪。

项目关系：本文件依赖 `core.sensitive`、`core.settings`、`memory.models`、`memory.repository` 等 5 个模块；被 `agent.query_agent`、`memory.consolidator`、`memory.evolution`、`memory.service` 等 6 个模块。
"""



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
    """函数功能：`_safe_error` 负责处理 safe error，服务于本文件职责：Memory Trace 存储与隐私裁剪。
    传参：
        error: 当前捕获的异常对象，类型为 `str | None`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
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
    """函数功能：`_read_traces` 负责读取 traces，服务于本文件职责：Memory Trace 存储与隐私裁剪。
    传参：
        path: 文件系统路径，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
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
    """函数功能：`start_trace` 负责启动 trace，服务于本文件职责：Memory Trace 存储与隐私裁剪。
    传参：
        trace_type: trace type 参数，由调用方传入，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str | None`，默认值为 `None`。
        query_len: query len 参数，由调用方传入，类型为 `int | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
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
    """函数功能：`add_step` 负责处理 add step，服务于本文件职责：Memory Trace 存储与隐私裁剪。
    传参：
        trace: trace 参数，由调用方传入，类型为 `dict[str, Any] | None`。
        step: step 参数，由调用方传入，类型为 `str`。
        status: status 参数，由调用方传入，类型为 `str`，默认值为 `'success'`。
        duration_ms: duration ms 参数，由调用方传入，类型为 `int`，默认值为 `0`。
        input_summary: input summary 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
        output_summary: output summary 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
        reason: reason 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        error: 当前捕获的异常对象，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`finish_trace` 负责追踪 finish，服务于本文件职责：Memory Trace 存储与隐私裁剪。
    传参：
        trace: trace 参数，由调用方传入，类型为 `dict[str, Any] | None`。
        status: status 参数，由调用方传入，类型为 `str`，默认值为 `'success'`。
        path: 文件系统路径，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
    """
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
    """函数功能：`latest_trace` 负责追踪 latest，服务于本文件职责：Memory Trace 存储与隐私裁剪。
    传参：
        path: 文件系统路径，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
    """
    traces = _read_traces(path)
    return traces[-1] if traces else None


def get_trace(trace_id: str, path: str | Path | None = None) -> dict[str, Any] | None:
    """函数功能：`get_trace` 负责获取 trace，服务于本文件职责：Memory Trace 存储与隐私裁剪。
    传参：
        trace_id: Trace 标识，用于读取或写入审计链路，类型为 `str`。
        path: 文件系统路径，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
    """
    for item in reversed(_read_traces(path)):
        if item.get("trace_id") == trace_id:
            return item
    return None


def find_traces_by_memory(memory_id: str, path: str | Path | None = None) -> list[dict[str, Any]]:
    """函数功能：`find_traces_by_memory` 负责查找 traces by memory，服务于本文件职责：Memory Trace 存储与隐私裁剪。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        path: 文件系统路径，类型为 `str | Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    matched = []
    for item in _read_traces(path):
        for step in item.get("steps", []):
            output = step.get("output_summary") or {}
            if output.get("memory_id") == memory_id or output.get("target_memory_id") == memory_id:
                matched.append(item)
                break
    return matched
