"""文件作用：本地结构化可观测性。

项目关系：本文件依赖 `core.sensitive`、`core.settings`、`infrastructure.redis_keys`；被 `agent.hooks.observability`、`agent.query_agent`、`apps.api`、`apps.outbox_relay` 等 17 个模块。
"""



from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from core.sensitive import assess_sensitive_text, redact_sensitive_text

LOG_DIR = Path("data/logs")
_LOCK = threading.RLock()
LOGGER = logging.getLogger(__name__)


def _safe_log_value(value: Any) -> Any:
    """函数功能：`_safe_log_value` 负责记录日志 value，服务于本文件职责：本地结构化可观测性。
    传参：
        value: 待转换、校验或计算的值，类型为 `Any`。
    返回结果说明：
        返回 `Any` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    if isinstance(value, str):
        if assess_sensitive_text(value).blocks_storage:
            return "[sensitive content redacted]"
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        return {str(key): _safe_log_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_log_value(item) for item in value]
    return value


def now_iso() -> str:
    """函数功能：`now_iso` 负责获取当前时间 iso，服务于本文件职责：本地结构化可观测性。
    传参：
        无。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _duration_ms(start: float) -> int:
    """函数功能：`_duration_ms` 负责处理 duration ms，服务于本文件职责：本地结构化可观测性。
    传参：
        start: start 参数，由调用方传入，类型为 `float`。
    返回结果说明：
        返回 `int`，表示计算得到的数值结果。
    """
    return int((time.perf_counter() - start) * 1000)


def _log_path() -> Path:
    """函数功能：`_log_path` 负责记录日志 path，服务于本文件职责：本地结构化可观测性。
    传参：
        无。
    返回结果说明：
        返回 `Path` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return LOG_DIR / f"app-{datetime.now().strftime('%Y-%m-%d')}.jsonl"


def log_event(
    action: str,
    *,
    level: str = "info",
    status: str = "success",
    space_id: str | None = None,
    message_id: str | None = None,
    record_id: str | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """函数功能：`log_event` 负责记录日志 event，服务于本文件职责：本地结构化可观测性。
    传参：
        action: action 参数，由调用方传入，类型为 `str`。
        level: level 参数，由调用方传入，类型为 `str`，默认值为 `'info'`。
        status: status 参数，由调用方传入，类型为 `str`，默认值为 `'success'`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str | None`，默认值为 `None`。
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str | None`，默认值为 `None`。
        record_id: record id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        duration_ms: duration ms 参数，由调用方传入，类型为 `int | None`，默认值为 `None`。
        error: 当前捕获的异常对象，类型为 `str | None`，默认值为 `None`。
        extra: extra 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    if os.getenv("SUIXINJI_OBSERVABILITY_DISABLED") == "1":
        return

    item = {
        "ts": now_iso(),
        "level": level,
        "action": action,
        "status": status,
        "space_id": space_id,
        "message_id": message_id,
        "record_id": record_id,
        "duration_ms": duration_ms,
        "error": _safe_log_value(error),
        "extra": _safe_log_value(extra or {}),
    }

    try:
        with _LOCK:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with _log_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception:
        LOGGER.exception("Failed to write observability event: %s", action)


def _code_revision() -> str | None:
    """函数功能：`_code_revision` 负责处理 code revision，服务于本文件职责：本地结构化可观测性。
    传参：
        无。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            check=False,
            text=True,
            timeout=1,
        )
    except Exception:
        return None
    value = result.stdout.strip()
    return value or None


def log_process_started(role: str | None = None, *, action: str = "runtime.process_started") -> None:
    """函数功能：`log_process_started` 负责记录日志 process started，服务于本文件职责：本地结构化可观测性。
    传参：
        role: role 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        action: action 参数，由调用方传入，类型为 `str`，默认值为 `'runtime.process_started'`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    from core.settings import (
        PROCESS_ROLE,
        REDIS_BLOCKING_SOCKET_TIMEOUT_SECONDS,
        REDIS_SOCKET_TIMEOUT_SECONDS,
        STREAM_BLOCK_MS,
        database_pool_budget,
    )
    from infrastructure.redis_keys import KEYS

    resolved_role = role or PROCESS_ROLE or "default"
    pool_size, max_overflow = database_pool_budget(resolved_role)
    log_event(
        action,
        status="started",
        extra={
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "process_role": PROCESS_ROLE,
            "role": resolved_role,
            "database_pool_size": pool_size,
            "database_max_overflow": max_overflow,
            "redis_namespace": KEYS.prefix,
            "stream_block_ms": STREAM_BLOCK_MS,
            "redis_socket_timeout_seconds": REDIS_SOCKET_TIMEOUT_SECONDS,
            "redis_blocking_socket_timeout_seconds": REDIS_BLOCKING_SOCKET_TIMEOUT_SECONDS,
            "code_revision": _code_revision(),
            "start_time": now_iso(),
        },
    )


@contextmanager
def observe(action: str, **ctx: Any) -> Iterator[None]:
    """函数功能：`observe` 负责处理 observe，服务于本文件职责：本地结构化可观测性。
    传参：
        action: action 参数，由调用方传入，类型为 `str`。
        **ctx: ctx 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `Iterator[None]` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    start = time.perf_counter()
    ctx_extra = ctx.pop("extra", None) or {}
    log_event(action, status="start", extra=ctx_extra, **ctx)
    try:
        yield
    except Exception as exc:
        error_extra = dict(ctx_extra)
        error_extra["traceback"] = traceback.format_exc()
        log_event(
            action,
            level="error",
            status="failed",
            duration_ms=_duration_ms(start),
            error=f"{type(exc).__name__}: {exc}",
            extra=error_extra,
            **ctx,
        )
        raise
    else:
        log_event(action, status="success", duration_ms=_duration_ms(start), extra=ctx_extra, **ctx)


def read_recent_events(limit: int = 100) -> list[dict[str, Any]]:
    """函数功能：`read_recent_events` 负责读取 recent events，服务于本文件职责：本地结构化可观测性。
    传参：
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `100`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    if not LOG_DIR.exists():
        return []

    events: list[dict[str, Any]] = []
    for path in sorted(LOG_DIR.glob("app-*.jsonl"), reverse=True):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue

        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(events) >= limit:
                return events

    return events


def recent_errors(limit: int = 5) -> list[dict[str, Any]]:
    """函数功能：`recent_errors` 负责处理 recent errors，服务于本文件职责：本地结构化可观测性。
    传参：
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `5`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    return [
        event
        for event in read_recent_events(limit=200)
        if event.get("level") == "error" or event.get("status") == "failed"
    ][:limit]


def latest_success(actions: set[str] | None = None) -> dict[str, Any] | None:
    """函数功能：`latest_success` 负责处理 latest success，服务于本文件职责：本地结构化可观测性。
    传参：
        actions: actions 参数，由调用方传入，类型为 `set[str] | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
    """
    for event in read_recent_events(limit=200):
        if event.get("status") != "success":
            continue
        if actions is not None and event.get("action") not in actions:
            continue
        return event
    return None
