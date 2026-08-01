"""文件作用：Redis Stream 消费器。

项目关系：本文件依赖 `core.observability`、`core.settings`、`repositories.postgres.tasks`、`runtime.streams.client`；被 `apps.handlers`、`apps.worker`、`tests.test_stage3_concurrency_ownership`、`tests.test_stage5_dispatch_performance` 等 6 个模块。
"""



from __future__ import annotations

import logging
import hashlib
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.observability import log_event
from core.settings import (
    PROCESS_ROLE,
    STREAM_CLAIM_IDLE_MS,
    STREAM_RECLAIM_INTERVAL_SECONDS,
    TASK_LEASE_SECONDS,
    WORKER_RETRY_BASE_SECONDS,
)
from repositories.postgres.tasks import claim_task, complete_task, defer_task, fail_task, get_task, renew_task_lease
from runtime.streams.client import GROUPS, StreamClient, StreamMessage

LOGGER = logging.getLogger(__name__)
HEARTBEAT_SESSION_ROLE = "worker-heartbeat"


@dataclass(frozen=True)
class TaskOutcome:
    """类功能：`TaskOutcome` 封装与“Redis Stream 消费器”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    release_inbox_id: str | None = None
    activate_task_id: str | None = None
    note_ready_inbox_id: str | None = None
    memory_ready_inbox_id: str | None = None
    ingest_complete_inbox_id: str | None = None

    def __post_init__(self) -> None:
        """函数功能：`TaskOutcome.__post_init__` 在类 `TaskOutcome` 中负责处理 post init，服务于本文件职责：Redis Stream 消费器。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        outcomes = (
            self.release_inbox_id,
            self.activate_task_id,
            self.note_ready_inbox_id,
            self.memory_ready_inbox_id,
            self.ingest_complete_inbox_id,
        )
        if sum(value is not None for value in outcomes) > 1:
            raise ValueError("a task may produce only one Inbox/dependency outcome")


TaskHandler = Callable[[dict[str, Any]], TaskOutcome | None]


def _elapsed_ms(start: datetime | None, end: datetime | None = None) -> int | None:
    """函数功能：`_elapsed_ms` 负责处理 elapsed ms，服务于本文件职责：Redis Stream 消费器。
    传参：
        start: start 参数，由调用方传入，类型为 `datetime | None`。
        end: end 参数，由调用方传入，类型为 `datetime | None`，默认值为 `None`。
    返回结果说明：
        返回 `int | None`；未命中或无需处理时可返回 `None`。
    """
    if start is None:
        return None
    end = end or datetime.now().astimezone()
    return max(0, int((end - start).total_seconds() * 1000))


def _safe_iso(value: Any) -> str | None:
    """函数功能：`_safe_iso` 负责处理 safe iso，服务于本文件职责：Redis Stream 消费器。
    传参：
        value: 待转换、校验或计算的值，类型为 `Any`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _lease_hash(value: str | None) -> str | None:
    """函数功能：`_lease_hash` 负责处理 lease hash，服务于本文件职责：Redis Stream 消费器。
    传参：
        value: 待转换、校验或计算的值，类型为 `str | None`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _default_outcome(task: dict[str, Any]) -> TaskOutcome:
    """函数功能：`_default_outcome` 负责处理 default outcome，服务于本文件职责：Redis Stream 消费器。
    传参：
        task: task 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `TaskOutcome` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    payload = dict(task.get("payload_json") or {})
    inbox_id = payload.get("inbox_id")
    return TaskOutcome(release_inbox_id=str(inbox_id)) if inbox_id else TaskOutcome()


class RetryLater(RuntimeError):
    """类功能：`RetryLater` 封装与“Redis Stream 消费器”相关的数据结构、状态或行为。
    继承关系：继承 `RuntimeError`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(self, message: str, delay_seconds: float = 1.0) -> None:
        """函数功能：`RetryLater.__init__` 在类 `RetryLater` 中负责初始化实例状态，服务于本文件职责：Redis Stream 消费器。
        传参：
            message: message 参数，由调用方传入，类型为 `str`。
            delay_seconds: delay seconds 参数，由调用方传入，类型为 `float`，默认值为 `1.0`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        super().__init__(message)
        self.delay_seconds = delay_seconds


class StreamWorker:
    """类功能：`StreamWorker` 封装与“Redis Stream 消费器”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(
        self,
        task_type: str,
        handler: TaskHandler,
        *,
        client: StreamClient | None = None,
        worker_id: str | None = None,
    ) -> None:
        """函数功能：`StreamWorker.__init__` 在类 `StreamWorker` 中负责初始化实例状态，服务于本文件职责：Redis Stream 消费器。
        传参：
            task_type: task type 参数，由调用方传入，类型为 `str`。
            handler: handler 参数，由调用方传入，类型为 `TaskHandler`。
            client: 外部服务或基础设施客户端，类型为 `StreamClient | None`，默认值为 `None`。
            worker_id: worker id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.task_type = task_type
        self.handler = handler
        self.client = client or StreamClient()
        self.worker_id = worker_id or f"{socket.gethostname()}-{task_type}-{uuid.uuid4().hex[:8]}"
        self.running = True
        stagger = (hash(self.worker_id) & 0xFFFF) / 0xFFFF * max(0.1, STREAM_RECLAIM_INTERVAL_SECONDS)
        self._next_reclaim_at = time.monotonic() + stagger

    def run_once(self, *, block_ms: int = 1000) -> int:
        """函数功能：`StreamWorker.run_once` 在类 `StreamWorker` 中负责运行 once，服务于本文件职责：Redis Stream 消费器。
        传参：
            block_ms: block ms 参数，由调用方传入，类型为 `int`，默认值为 `1000`。
        返回结果说明：
            返回 `int`，表示计算得到的数值结果。
        """
        messages = self.client.read(self.task_type, self.worker_id, block_ms=block_ms)
        if not messages:
            messages = self._reclaim_if_due()
        for message in messages:
            self._handle(message)
        return len(messages)

    def _reclaim_if_due(self) -> list[StreamMessage]:
        """函数功能：`StreamWorker._reclaim_if_due` 在类 `StreamWorker` 中负责处理 reclaim if due，服务于本文件职责：Redis Stream 消费器。
        传参：
            无。
        返回结果说明：
            返回 `list[StreamMessage]`，表示按条件筛选、构造或查询得到的列表。
        """
        now = time.monotonic()
        if now < self._next_reclaim_at:
            return []
        reclaim_started = time.perf_counter()
        messages = self.client.reclaim(self.task_type, self.worker_id, min_idle_ms=STREAM_CLAIM_IDLE_MS)
        self._next_reclaim_at = now + max(0.1, STREAM_RECLAIM_INTERVAL_SECONDS)
        log_event(
            "runtime.stream_reclaim",
            status="completed",
            duration_ms=int((time.perf_counter() - reclaim_started) * 1000),
            extra={
                "task_type": self.task_type,
                "worker_id": self.worker_id,
                "reclaim_count": len(messages),
                "next_start_id": self.client.reclaim_cursor(self.task_type, self.worker_id),
                "min_idle_ms": STREAM_CLAIM_IDLE_MS,
            },
        )
        if messages:
            log_event(
                "runtime.stream_pending_reclaimed",
                status="reclaimed",
                extra={
                    "task_type": self.task_type,
                    "worker_id": self.worker_id,
                    "reclaim_count": len(messages),
                    "next_start_id": self.client.reclaim_cursor(self.task_type, self.worker_id),
                    "min_idle_ms": STREAM_CLAIM_IDLE_MS,
                    "redis_message_ids": [message.message_id for message in messages],
                },
            )
        return messages

    def run_forever(self) -> None:
        """函数功能：`StreamWorker.run_forever` 在类 `StreamWorker` 中负责运行 forever，服务于本文件职责：Redis Stream 消费器。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        while self.running:
            try:
                self.run_once()
            except Exception:
                LOGGER.exception("stream worker loop failed: type=%s worker=%s", self.task_type, self.worker_id)
                time.sleep(1)

    def stop(self) -> None:
        """函数功能：`StreamWorker.stop` 在类 `StreamWorker` 中负责停止，服务于本文件职责：Redis Stream 消费器。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.running = False

    def _handle(self, message: StreamMessage) -> None:
        """函数功能：`StreamWorker._handle` 在类 `StreamWorker` 中负责处理，服务于本文件职责：Redis Stream 消费器。
        传参：
            message: message 参数，由调用方传入，类型为 `StreamMessage`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        task_id = str(message.fields.get("task_id") or "")
        stream_extra = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "process_role": PROCESS_ROLE,
            "worker_id": self.worker_id,
            "task_id": task_id or None,
            "task_type": self.task_type,
            "stream": message.stream,
            "consumer_group": GROUPS.get(self.task_type),
            "consumer": self.worker_id,
            "redis_message_id": message.message_id,
        }
        log_event(
            "runtime.stream_message_received",
            status="received",
            record_id=task_id or None,
            extra=stream_extra,
        )
        if not task_id:
            self.client.dead_letter(message, error="missing task_id")
            self.client.ack(self.task_type, message.message_id)
            return
        task = claim_task(task_id, self.worker_id, stale_after_seconds=TASK_LEASE_SECONDS)
        if task is None:
            existing = get_task(task_id)
            if existing is None or existing.get("status") in {
                "blocked",
                "cancelled",
                "completed",
                "dead_letter",
                "retry",
            }:
                self.client.ack(self.task_type, message.message_id)
            log_event(
                "runtime.task_claim_skipped",
                status=str((existing or {}).get("status") or "missing"),
                record_id=task_id,
                extra={**stream_extra, "previous_status": (existing or {}).get("status")},
            )
            return
        lease_token = str(task["lease_token"])
        claim_version = int(task["claim_version"])
        stop_heartbeat = threading.Event()
        ownership_lost = threading.Event()

        def renew_lease() -> None:
            """函数功能：`StreamWorker.renew_lease` 在类 `StreamWorker` 中负责处理 renew lease，服务于本文件职责：Redis Stream 消费器。
            传参：
                无。
            返回结果说明：
                无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
            """
            interval = max(0.5, TASK_LEASE_SECONDS / 3)
            while not stop_heartbeat.wait(interval):
                renew_started = time.perf_counter()
                try:
                    renewed = renew_task_lease(
                        task_id,
                        lease_token=lease_token,
                        claim_version=claim_version,
                        lease_seconds=TASK_LEASE_SECONDS,
                        session_role=HEARTBEAT_SESSION_ROLE,
                    )
                    if renewed:
                        log_event(
                            "runtime.task_lease_renewed",
                            status="success",
                            space_id=str(task.get("space_id") or "") or None,
                            message_id=task.get("source_message_id"),
                            record_id=task_id,
                            duration_ms=int((time.perf_counter() - renew_started) * 1000),
                            extra=common_extra,
                        )
                    else:
                        ownership_lost.set()
                        log_event(
                            "runtime.task_lease_renew_failed",
                            level="warning",
                            status="stale",
                            space_id=str(task.get("space_id") or "") or None,
                            message_id=task.get("source_message_id"),
                            record_id=task_id,
                            duration_ms=int((time.perf_counter() - renew_started) * 1000),
                            extra=common_extra,
                        )
                        return
                except Exception as exc:
                    log_event(
                        "runtime.task_lease_renew_failed",
                        level="warning",
                        status="failed",
                        space_id=str(task.get("space_id") or "") or None,
                        message_id=task.get("source_message_id"),
                        record_id=task_id,
                        duration_ms=int((time.perf_counter() - renew_started) * 1000),
                        error=type(exc).__name__,
                        extra=common_extra,
                    )
                    LOGGER.warning("task lease renewal failed: task_id=%s", task_id, exc_info=True)

        execution_started = time.perf_counter()
        queue_wait_ms = _elapsed_ms(task.get("created_at"), task.get("started_at"))
        common_extra = {
            **stream_extra,
            "task_id": task_id,
            "task_type": self.task_type,
            "worker_id": self.worker_id,
            "attempt_count": int(task.get("attempt_count") or 0),
            "failure_count": int(task.get("failure_count") or 0),
            "defer_count": int(task.get("defer_count") or 0),
            "queue_wait_ms": queue_wait_ms,
            "claim_version": claim_version,
            "lease_token_hash": _lease_hash(lease_token),
            "lease_expires_at": _safe_iso(task.get("lease_expires_at")),
            "previous_status": task.get("previous_status"),
            "previous_claimed_by": task.get("previous_claimed_by"),
            "previous_lease_expires_at": _safe_iso(task.get("previous_lease_expires_at")),
        }
        log_event(
            "runtime.task_claimed",
            status="running",
            space_id=str(task.get("space_id") or "") or None,
            message_id=task.get("source_message_id"),
            record_id=task_id,
            extra=common_extra,
        )
        if task.get("previous_status") == "running":
            log_event(
                "runtime.task_lease_reclaimed",
                status="reclaimed",
                space_id=str(task.get("space_id") or "") or None,
                message_id=task.get("source_message_id"),
                record_id=task_id,
                extra={**common_extra, "reclaim_reason": "lease_expired"},
            )
        log_event(
            "runtime.stream_task_started",
            status="running",
            space_id=str(task.get("space_id") or "") or None,
            message_id=task.get("source_message_id"),
            record_id=task_id,
            extra=common_extra,
        )

        heartbeat = threading.Thread(target=renew_lease, name=f"task-lease-{task_id[-8:]}", daemon=True)
        heartbeat.start()

        def finish_heartbeat() -> None:
            """函数功能：`StreamWorker.finish_heartbeat` 在类 `StreamWorker` 中负责处理 finish heartbeat，服务于本文件职责：Redis Stream 消费器。
            传参：
                无。
            返回结果说明：
                无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
            """
            stop_heartbeat.set()
            heartbeat.join(timeout=1)

        try:
            outcome = self.handler(task)
            if outcome is None:
                outcome = _default_outcome(task)
            elif not isinstance(outcome, TaskOutcome):
                raise TypeError(f"task handler returned unsupported outcome: {type(outcome).__name__}")
        except RetryLater as exc:
            finish_heartbeat()
            deferred = defer_task(
                task_id,
                str(exc),
                retry_delay_seconds=exc.delay_seconds,
                lease_token=lease_token,
                claim_version=claim_version,
            )
            if deferred:
                self.client.ack(self.task_type, message.message_id)
            execution_ms = int((time.perf_counter() - execution_started) * 1000)
            log_event(
                "runtime.stream_task_deferred",
                level="warning",
                status="retry" if deferred else "stale",
                space_id=str(task.get("space_id") or "") or None,
                message_id=task.get("source_message_id"),
                record_id=task_id,
                duration_ms=execution_ms,
                error=type(exc).__name__,
                extra={
                    **common_extra,
                    "defer_count": int(task.get("defer_count") or 0) + 1,
                    "execution_ms": execution_ms,
                    "retry_delay_seconds": exc.delay_seconds,
                },
            )
            log_event(
                "runtime.task_retry_scheduled",
                level="warning",
                status="retry" if deferred else "stale",
                space_id=str(task.get("space_id") or "") or None,
                message_id=task.get("source_message_id"),
                record_id=task_id,
                duration_ms=execution_ms,
                error=type(exc).__name__,
                extra={
                    **common_extra,
                    "defer_count": int(task.get("defer_count") or 0) + 1,
                    "execution_ms": execution_ms,
                    "retry_delay_seconds": exc.delay_seconds,
                    "reclaim_reason": "handler_deferred",
                },
            )
            return
        except Exception as exc:
            finish_heartbeat()
            failure_no = int(task.get("failure_count") or 0) + 1
            delay = WORKER_RETRY_BASE_SECONDS * (2 ** max(0, failure_no - 1))
            error = f"{type(exc).__name__}: {exc}"
            status = fail_task(
                task_id,
                error,
                retry_delay_seconds=delay,
                lease_token=lease_token,
                claim_version=claim_version,
            )
            if status == "dead_letter":
                self.client.dead_letter(message, error=error)
            if status != "stale":
                self.client.ack(self.task_type, message.message_id)
            execution_ms = int((time.perf_counter() - execution_started) * 1000)
            log_event(
                "runtime.stream_task_failed",
                level="error",
                status=status,
                space_id=str(task.get("space_id") or "") or None,
                message_id=task.get("source_message_id"),
                record_id=task_id,
                duration_ms=execution_ms,
                error=type(exc).__name__,
                extra={
                    **common_extra,
                    "failure_count": failure_no,
                    "execution_ms": execution_ms,
                    "retry_delay_seconds": delay,
                },
            )
            log_event(
                "runtime.task_failed",
                level="error",
                status=status,
                space_id=str(task.get("space_id") or "") or None,
                message_id=task.get("source_message_id"),
                record_id=task_id,
                duration_ms=execution_ms,
                error=type(exc).__name__,
                extra={
                    **common_extra,
                    "failure_count": failure_no,
                    "execution_ms": execution_ms,
                    "retry_delay_seconds": delay,
                },
            )
            if status == "retry":
                log_event(
                    "runtime.task_retry_scheduled",
                    level="warning",
                    status="retry",
                    space_id=str(task.get("space_id") or "") or None,
                    message_id=task.get("source_message_id"),
                    record_id=task_id,
                    duration_ms=execution_ms,
                    error=type(exc).__name__,
                    extra={
                        **common_extra,
                        "failure_count": failure_no,
                        "execution_ms": execution_ms,
                        "retry_delay_seconds": delay,
                        "reclaim_reason": "handler_failed",
                    },
                )
            elif status == "dead_letter":
                log_event(
                    "runtime.task_dead_lettered",
                    level="error",
                    status="dead_letter",
                    space_id=str(task.get("space_id") or "") or None,
                    message_id=task.get("source_message_id"),
                    record_id=task_id,
                    duration_ms=execution_ms,
                    error=type(exc).__name__,
                    extra={
                        **common_extra,
                        "failure_count": failure_no,
                        "execution_ms": execution_ms,
                    },
                )
            LOGGER.exception("stream task failed: task_id=%s task_type=%s status=%s", task_id, self.task_type, status)
            return
        finish_heartbeat()
        completed = complete_task(
            task_id,
            lease_token=lease_token,
            claim_version=claim_version,
            release_inbox_id=outcome.release_inbox_id,
            activate_task_id=outcome.activate_task_id,
            note_ready_inbox_id=outcome.note_ready_inbox_id,
            memory_ready_inbox_id=outcome.memory_ready_inbox_id,
            ingest_complete_inbox_id=outcome.ingest_complete_inbox_id,
        )
        if completed:
            self.client.ack(self.task_type, message.message_id)
        finished_at = datetime.now().astimezone()
        execution_ms = int((time.perf_counter() - execution_started) * 1000)
        log_event(
            "runtime.stream_task_completed",
            status="completed" if completed else "stale",
            space_id=str(task.get("space_id") or "") or None,
            message_id=task.get("source_message_id"),
            record_id=task_id,
            duration_ms=execution_ms,
            extra={
                **common_extra,
                "ownership_lost": ownership_lost.is_set() or not completed,
                "execution_ms": execution_ms,
                "total_duration_ms": _elapsed_ms(task.get("created_at"), finished_at),
            },
        )
        log_event(
            "runtime.task_completed" if completed else "runtime.task_stale_completion",
            level="info" if completed else "warning",
            status="completed" if completed else "stale",
            space_id=str(task.get("space_id") or "") or None,
            message_id=task.get("source_message_id"),
            record_id=task_id,
            duration_ms=execution_ms,
            extra={
                **common_extra,
                "ownership_lost": ownership_lost.is_set() or not completed,
                "execution_ms": execution_ms,
                "total_duration_ms": _elapsed_ms(task.get("created_at"), finished_at),
            },
        )


class AdaptiveStreamWorker:
    """类功能：`AdaptiveStreamWorker` 封装与“Redis Stream 消费器”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """

    def __init__(
        self,
        handlers: dict[str, TaskHandler],
        *,
        client: StreamClient | None = None,
        worker_id: str | None = None,
    ) -> None:
        """函数功能：`AdaptiveStreamWorker.__init__` 在类 `AdaptiveStreamWorker` 中负责初始化实例状态，服务于本文件职责：Redis Stream 消费器。
        传参：
            handlers: handlers 参数，由调用方传入，类型为 `dict[str, TaskHandler]`。
            client: 外部服务或基础设施客户端，类型为 `StreamClient | None`，默认值为 `None`。
            worker_id: worker id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.client = client or StreamClient()
        self.worker_id = worker_id or f"{socket.gethostname()}-adaptive-{uuid.uuid4().hex[:8]}"
        self.task_types = list(handlers)
        if not self.task_types:
            raise ValueError("adaptive worker requires at least one task handler")
        self.workers = {
            task_type: StreamWorker(
                task_type,
                handler,
                client=self.client,
                worker_id=self.worker_id,
            )
            for task_type, handler in handlers.items()
        }
        self.running = True
        self.foreground_task_types = [
            task_type for task_type in self.task_types if task_type not in {"delivery", "enrichment"}
        ]
        self.background_task_types = [
            task_type for task_type in self.task_types if task_type in {"delivery", "enrichment"}
        ]
        self._foreground_cursor = 0
        self._background_cursor = 0
        self._foreground_batches = 0

    @staticmethod
    def _rotated(task_types: list[str], cursor: int) -> list[str]:
        """函数功能：`AdaptiveStreamWorker._rotated` 在类 `AdaptiveStreamWorker` 中负责处理 rotated，服务于本文件职责：Redis Stream 消费器。
        传参：
            task_types: task types 参数，由调用方传入，类型为 `list[str]`。
            cursor: cursor 参数，由调用方传入，类型为 `int`。
        返回结果说明：
            返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
        """
        return task_types[cursor:] + task_types[:cursor]

    def _handle_messages(self, messages: list[StreamMessage]) -> None:
        """函数功能：`AdaptiveStreamWorker._handle_messages` 在类 `AdaptiveStreamWorker` 中负责处理 messages，服务于本文件职责：Redis Stream 消费器。
        传参：
            messages: messages 参数，由调用方传入，类型为 `list[StreamMessage]`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        for message in messages:
            task_type = str(message.fields.get("task_type") or "")
            worker = self.workers.get(task_type)
            if worker is None:
                self.client.dead_letter(message, error=f"unsupported task_type: {task_type}")
                continue
            worker._handle(message)

    def run_once(self) -> int:
        """函数功能：`AdaptiveStreamWorker.run_once` 在类 `AdaptiveStreamWorker` 中负责运行 once，服务于本文件职责：Redis Stream 消费器。
        传参：
            无。
        返回结果说明：
            返回 `int`，表示计算得到的数值结果。
        """
        foreground = self._rotated(self.foreground_task_types, self._foreground_cursor)
        background = self._rotated(self.background_task_types, self._background_cursor)
        lanes = [(foreground, True), (background, False)]
        if background and self._foreground_batches >= 4:
            lanes.reverse()
        for lane, is_foreground in lanes:
            if not lane:
                continue
            messages = self.client.read_many(lane, self.worker_id, count=1)
            if not messages:
                continue
            self._handle_messages(messages)
            if is_foreground:
                self._foreground_cursor = (self._foreground_cursor + 1) % len(self.foreground_task_types)
                self._foreground_batches += 1
            else:
                self._background_cursor = (self._background_cursor + 1) % len(self.background_task_types)
                self._foreground_batches = 0
            return len(messages)
        ordered = self._rotated(self.task_types, self._foreground_cursor % len(self.task_types))
        for task_type in ordered:
            reclaimed = self.workers[task_type]._reclaim_if_due()
            if not reclaimed:
                continue
            for message in reclaimed:
                self.workers[task_type]._handle(message)
            return len(reclaimed)
        return 0

    def run_forever(self) -> None:
        """函数功能：`AdaptiveStreamWorker.run_forever` 在类 `AdaptiveStreamWorker` 中负责运行 forever，服务于本文件职责：Redis Stream 消费器。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        idle_sleep = 0.02
        while self.running:
            try:
                if self.run_once() == 0:
                    time.sleep(idle_sleep)
                    idle_sleep = min(0.25, idle_sleep * 2)
                else:
                    idle_sleep = 0.02
            except Exception:
                LOGGER.exception("adaptive stream worker loop failed: worker=%s", self.worker_id)
                idle_sleep = 0.02
                time.sleep(1)

    def stop(self) -> None:
        """函数功能：`AdaptiveStreamWorker.stop` 在类 `AdaptiveStreamWorker` 中负责停止，服务于本文件职责：Redis Stream 消费器。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.running = False
