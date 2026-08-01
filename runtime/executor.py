"""文件作用：本地有界异步执行器。

项目关系：本文件依赖 `agent.query_agent`、`core.file_lock`、`core.observability`、`core.settings` 等 9 个模块；被 `bot.feishu_bot`、`runtime.enrichment_drainer`、`runtime.pending_drainer`、`summary.scheduler` 等 8 个模块。
"""



from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any

from agent.query_agent import answer_question
from core.file_lock import get_space_lock
from core.observability import log_event
from core.settings import ENRICHMENT_MAX_WORKERS, ENRICHMENT_QUEUE_SIZE, MAX_WORKERS, TASK_QUEUE_SIZE
from core.worker import enrich_note, process_pending, process_record
from runtime.delivery_store import (
    ingest_archived_key,
    manual_summary_key,
    mark_failed,
    mark_sent,
    mark_unknown,
    query_key,
    reserve_delivery,
)
from runtime.task import Task, create_task
from runtime.task_registry import TaskRegistry
from summary.daily_summary import generate_summary

LOGGER = logging.getLogger(__name__)
SendText = Callable[[str, str], bool]


class BoundedTaskExecutor:
    """类功能：`BoundedTaskExecutor` 封装与“本地有界异步执行器”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(
        self,
        *,
        max_workers: int = MAX_WORKERS,
        queue_size: int = TASK_QUEUE_SIZE,
        send_text: SendText | None = None,
    ) -> None:
        """函数功能：`BoundedTaskExecutor.__init__` 在类 `BoundedTaskExecutor` 中负责初始化实例状态，服务于本文件职责：本地有界异步执行器。
        传参：
            max_workers: max workers 参数，由调用方传入，类型为 `int`，默认值为 `MAX_WORKERS`。
            queue_size: queue size 参数，由调用方传入，类型为 `int`，默认值为 `TASK_QUEUE_SIZE`。
            send_text: send text 参数，由调用方传入，类型为 `SendText | None`，默认值为 `None`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="suixinji-task")
        self._enrichment_pool = ThreadPoolExecutor(
            max_workers=ENRICHMENT_MAX_WORKERS,
            thread_name_prefix="suixinji-enrichment",
        )
        self._enrichment_slots = threading.BoundedSemaphore(ENRICHMENT_MAX_WORKERS + ENRICHMENT_QUEUE_SIZE)
        self._slots = threading.BoundedSemaphore(max_workers + queue_size)
        self._max_workers = max_workers
        self._queue_size = queue_size
        self._capacity = max_workers + queue_size
        self._registry = TaskRegistry()
        self._send_text = send_text
        self._summary_locks: dict[str, threading.Lock] = {}
        self._summary_locks_guard = threading.Lock()
        self._inflight_ingest_keys: set[tuple[str, str]] = set()
        self._inflight_ingest_lock = threading.Lock()
        self._inflight_enrichment: set[tuple[str, str]] = set()
        self._inflight_enrichment_lock = threading.Lock()
        self._shutdown = False
        self._shutdown_lock = threading.Lock()

    def set_send_text(self, send_text: SendText) -> None:
        """函数功能：`BoundedTaskExecutor.set_send_text` 在类 `BoundedTaskExecutor` 中负责设置 send text，服务于本文件职责：本地有界异步执行器。
        传参：
            send_text: send text 参数，由调用方传入，类型为 `SendText`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self._send_text = send_text

    def submit_ingest(
        self,
        record: Any,
        chat_id: str | None = None,
        *,
        notify_on_success: bool = False,
        source: str = "direct",
    ) -> Task:
        """函数功能：`BoundedTaskExecutor.submit_ingest` 在类 `BoundedTaskExecutor` 中负责处理 submit ingest，服务于本文件职责：本地有界异步执行器。
        传参：
            record: 待处理或持久化的记录对象，类型为 `Any`。
            chat_id: chat id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
            notify_on_success: notify on success 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
            source: source 参数，由调用方传入，类型为 `str`，默认值为 `'direct'`。
        返回结果说明：
            返回 `Task` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        record_dict = _record_to_dict(record)
        message_id = str(record_dict.get("message_id") or "")
        task = create_task("ingest", record_dict["space_id"], {}, message_id=message_id or None)
        inflight_key = (task.space_id, message_id)
        reserved_inflight = False
        if message_id:
            reserved_inflight = self._reserve_inflight_ingest(inflight_key)
            if not reserved_inflight:
                return self._reject(task, "ingest already in flight")

        payload = {
            "record": record_dict,
            "chat_id": chat_id,
            "notify_on_success": notify_on_success,
            "source": source,
        }
        task.payload = payload
        submitted = self._submit(
            task,
            self._run_ingest,
            on_finished=lambda: self._release_inflight_ingest(inflight_key) if reserved_inflight else None,
        )
        if submitted.status == "rejected" and reserved_inflight:
            self._release_inflight_ingest(inflight_key)
        return submitted

    def submit_query(self, space_id: str, question: str, chat_id: str, message_id: str | None = None) -> Task:
        """函数功能：`BoundedTaskExecutor.submit_query` 在类 `BoundedTaskExecutor` 中负责查询 submit，服务于本文件职责：本地有界异步执行器。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            question: 用户问题文本，类型为 `str`。
            chat_id: chat id 参数，由调用方传入，类型为 `str`。
            message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str | None`，默认值为 `None`。
        返回结果说明：
            返回 `Task` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        delivery_key = query_key(space_id, message_id) if message_id else query_key(space_id, "unknown")
        task = create_task(
            "query",
            space_id,
            {
                "question": question,
                "chat_id": chat_id,
                "delivery_key": delivery_key,
                "delivery_type": "query",
            },
            message_id=message_id,
        )
        return self._submit(task, self._run_query)

    def submit_summary(
        self,
        space_id: str,
        range_key: str,
        chat_id: str,
        message_id: str | None = None,
        on_success: Callable[[], None] | None = None,
        delivery_key: str | None = None,
        delivery_type: str | None = None,
    ) -> Task:
        """函数功能：`BoundedTaskExecutor.submit_summary` 在类 `BoundedTaskExecutor` 中负责处理 submit summary，服务于本文件职责：本地有界异步执行器。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            range_key: range key 参数，由调用方传入，类型为 `str`。
            chat_id: chat id 参数，由调用方传入，类型为 `str`。
            message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str | None`，默认值为 `None`。
            on_success: on success 参数，由调用方传入，类型为 `Callable[[], None] | None`，默认值为 `None`。
            delivery_key: delivery key 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
            delivery_type: delivery type 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        返回结果说明：
            返回 `Task` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        if delivery_key is None and message_id:
            delivery_key = manual_summary_key(space_id, message_id)
        task = create_task(
            "summary",
            space_id,
            {
                "range_key": range_key,
                "chat_id": chat_id,
                "on_success": on_success,
                "delivery_key": delivery_key,
                "delivery_type": delivery_type or "manual_summary",
            },
            message_id=message_id,
        )
        return self._submit(task, self._run_summary)

    def get_stats(self) -> dict[str, Any]:
        """函数功能：`BoundedTaskExecutor.get_stats` 在类 `BoundedTaskExecutor` 中负责获取 stats，服务于本文件职责：本地有界异步执行器。
        传参：
            无。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
        stats = self._registry.get_stats()
        stats.update(
            {
                "max_workers": self._max_workers,
                "queue_size": self._queue_size,
                "capacity": self._capacity,
                "remaining_slots": self.remaining_slots(),
                "inflight_ingest": self.inflight_ingest_count(),
                "inflight_enrichment": self.inflight_enrichment_count(),
            }
        )
        return stats

    def remaining_slots(self) -> int:
        """函数功能：`BoundedTaskExecutor.remaining_slots` 在类 `BoundedTaskExecutor` 中负责处理 remaining slots，服务于本文件职责：本地有界异步执行器。
        传参：
            无。
        返回结果说明：
            返回 `int`，表示计算得到的数值结果。
        """
        return int(getattr(self._slots, "_value", 0))

    def has_inflight_ingest(self, space_id: str, message_id: str) -> bool:
        """函数功能：`BoundedTaskExecutor.has_inflight_ingest` 在类 `BoundedTaskExecutor` 中负责判断是否包含 inflight ingest，服务于本文件职责：本地有界异步执行器。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        with self._inflight_ingest_lock:
            return (space_id, message_id) in self._inflight_ingest_keys

    def inflight_ingest_count(self) -> int:
        """函数功能：`BoundedTaskExecutor.inflight_ingest_count` 在类 `BoundedTaskExecutor` 中负责计数 inflight ingest，服务于本文件职责：本地有界异步执行器。
        传参：
            无。
        返回结果说明：
            返回 `int`，表示计算得到的数值结果。
        """
        with self._inflight_ingest_lock:
            return len(self._inflight_ingest_keys)

    def inflight_enrichment_count(self) -> int:
        """函数功能：`BoundedTaskExecutor.inflight_enrichment_count` 在类 `BoundedTaskExecutor` 中负责计数 inflight enrichment，服务于本文件职责：本地有界异步执行器。
        传参：
            无。
        返回结果说明：
            返回 `int`，表示计算得到的数值结果。
        """
        with self._inflight_enrichment_lock:
            return len(self._inflight_enrichment)

    def submit_enrichment(self, space_id: str, note_id: str) -> bool:
        """函数功能：`BoundedTaskExecutor.submit_enrichment` 在类 `BoundedTaskExecutor` 中负责处理 submit enrichment，服务于本文件职责：本地有界异步执行器。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            note_id: Note 标识，用于定位原始记录，类型为 `str`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        key = (str(space_id), str(note_id))
        if not key[0] or not key[1]:
            return False
        with self._shutdown_lock:
            if self._shutdown:
                return False
        with self._inflight_enrichment_lock:
            if key in self._inflight_enrichment:
                return False
            if not self._enrichment_slots.acquire(blocking=False):
                return False
            self._inflight_enrichment.add(key)
        self._enrichment_pool.submit(self._run_enrichment, key)
        return True

    def shutdown(self) -> None:
        """函数功能：`BoundedTaskExecutor.shutdown` 在类 `BoundedTaskExecutor` 中负责处理 shutdown，服务于本文件职责：本地有界异步执行器。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        with self._shutdown_lock:
            self._shutdown = True
        self._pool.shutdown(wait=True)
        self._enrichment_pool.shutdown(wait=True)

    def _submit(
        self,
        task: Task,
        runner: Callable[[Task], None],
        *,
        on_finished: Callable[[], None] | None = None,
    ) -> Task:
        """函数功能：`BoundedTaskExecutor._submit` 在类 `BoundedTaskExecutor` 中负责处理 submit，服务于本文件职责：本地有界异步执行器。
        传参：
            task: task 参数，由调用方传入，类型为 `Task`。
            runner: runner 参数，由调用方传入，类型为 `Callable[[Task], None]`。
            on_finished: on finished 参数，由调用方传入，类型为 `Callable[[], None] | None`，默认值为 `None`。
        返回结果说明：
            返回 `Task` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        with self._shutdown_lock:
            if self._shutdown:
                return self._reject(task, "executor is shutting down")

        if not self._slots.acquire(blocking=False):
            return self._reject(task, "task queue is full")

        self._registry.add(task)
        log_event(
            "runtime.task_queued",
            space_id=task.space_id,
            message_id=task.message_id,
            record_id=task.id,
            extra={"task_type": task.task_type},
        )
        self._pool.submit(self._run_task, task, runner, on_finished)
        return task

    def _reject(self, task: Task, error: str) -> Task:
        """函数功能：`BoundedTaskExecutor._reject` 在类 `BoundedTaskExecutor` 中负责拒绝，服务于本文件职责：本地有界异步执行器。
        传参：
            task: task 参数，由调用方传入，类型为 `Task`。
            error: 当前捕获的异常对象，类型为 `str`。
        返回结果说明：
            返回 `Task` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        rejected = self._registry.reject(task, error)
        log_event(
            "runtime.task_rejected",
            level="warning",
            status="rejected",
            space_id=task.space_id,
            message_id=task.message_id,
            record_id=task.id,
            error=error,
            extra={"task_type": task.task_type},
        )
        return rejected

    def _run_task(self, task: Task, runner: Callable[[Task], None], on_finished: Callable[[], None] | None = None) -> None:
        """函数功能：`BoundedTaskExecutor._run_task` 在类 `BoundedTaskExecutor` 中负责运行 task，服务于本文件职责：本地有界异步执行器。
        传参：
            task: task 参数，由调用方传入，类型为 `Task`。
            runner: runner 参数，由调用方传入，类型为 `Callable[[Task], None]`。
            on_finished: on finished 参数，由调用方传入，类型为 `Callable[[], None] | None`，默认值为 `None`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self._registry.mark_running(task.id)
        log_event(
            "runtime.task_running",
            space_id=task.space_id,
            message_id=task.message_id,
            record_id=task.id,
            extra={"task_type": task.task_type},
        )
        try:
            runner(task)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            LOGGER.exception("Task failed: type=%s id=%s", task.task_type, task.id)
            self._registry.mark_failed(task.id, error)
            log_event(
                "runtime.task_failed",
                level="error",
                status="failed",
                space_id=task.space_id,
                message_id=task.message_id,
                record_id=task.id,
                error=error,
                extra={"task_type": task.task_type, **_task_timing(task)},
            )
        else:
            self._registry.mark_success(task.id)
            log_event(
                "runtime.task_success",
                space_id=task.space_id,
                message_id=task.message_id,
                record_id=task.id,
                extra={"task_type": task.task_type, **_task_timing(task)},
            )
        finally:
            if on_finished is not None:
                on_finished()
            self._slots.release()

    def _run_ingest(self, task: Task) -> None:
        """函数功能：`BoundedTaskExecutor._run_ingest` 在类 `BoundedTaskExecutor` 中负责运行 ingest，服务于本文件职责：本地有界异步执行器。
        传参：
            task: task 参数，由调用方传入，类型为 `Task`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        record = task.payload["record"]
        chat_id = task.payload.get("chat_id")
        with get_space_lock(task.space_id):
            note = process_record(record)
        if chat_id and task.payload.get("notify_on_success"):
            message_id = str(record.get("message_id") or task.message_id or task.id)
            self._deliver(
                chat_id,
                "已归档到随心记。",
                delivery_key=ingest_archived_key(task.space_id, message_id),
                delivery_type="ingest_archived",
                task=task,
            )
        note_id = _note_id(note)
        if note_id:
            self.submit_enrichment(task.space_id, note_id)

    def _run_query(self, task: Task) -> None:
        """函数功能：`BoundedTaskExecutor._run_query` 在类 `BoundedTaskExecutor` 中负责运行 query，服务于本文件职责：本地有界异步执行器。
        传参：
            task: task 参数，由调用方传入，类型为 `Task`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        question = str(task.payload["question"])
        chat_id = str(task.payload["chat_id"])
        try:
            # 查询可能与前一个 ingest 任务竞争；读取 Note 前先把同一 space 快速推进到本地归档阶段。
            with get_space_lock(task.space_id):
                process_pending(task.space_id)
            answer = answer_question(task.space_id, question)
        except Exception:
            self._deliver_query_failure_notice(chat_id, task)
            raise
        self._deliver(
            chat_id,
            answer,
            delivery_key=str(task.payload["delivery_key"]),
            delivery_type=str(task.payload["delivery_type"]),
            task=task,
        )

    def _run_enrichment(self, key: tuple[str, str]) -> None:
        """函数功能：`BoundedTaskExecutor._run_enrichment` 在类 `BoundedTaskExecutor` 中负责运行 enrichment，服务于本文件职责：本地有界异步执行器。
        传参：
            key: key 参数，由调用方传入，类型为 `tuple[str, str]`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        space_id, note_id = key
        log_event("worker.enrichment_queued", space_id=space_id, record_id=note_id)
        try:
            changed = enrich_note(space_id, note_id)
        except Exception as exc:
            LOGGER.exception("Background enrichment failed: space_id=%s note_id=%s", space_id, note_id)
            log_event(
                "worker.enrichment_failed",
                level="warning",
                status="failed",
                space_id=space_id,
                record_id=note_id,
                error=type(exc).__name__,
            )
        else:
            log_event(
                "worker.enrichment_finished",
                status="success" if changed else "skipped",
                space_id=space_id,
                record_id=note_id,
            )
        finally:
            with self._inflight_enrichment_lock:
                self._inflight_enrichment.discard(key)
            self._enrichment_slots.release()

    def _run_summary(self, task: Task) -> None:
        """函数功能：`BoundedTaskExecutor._run_summary` 在类 `BoundedTaskExecutor` 中负责运行 summary，服务于本文件职责：本地有界异步执行器。
        传参：
            task: task 参数，由调用方传入，类型为 `Task`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        range_key = str(task.payload["range_key"])
        chat_id = str(task.payload["chat_id"])
        lock = self._summary_lock(task.space_id)
        with lock:
            result = generate_summary(task.space_id, range_key)
            delivery_key = task.payload.get("delivery_key")
            if not delivery_key:
                delivery_key = manual_summary_key(task.space_id, task.message_id or task.id)
            self._deliver(
                chat_id,
                result.markdown,
                delivery_key=str(delivery_key),
                delivery_type=str(task.payload.get("delivery_type") or "manual_summary"),
                task=task,
                on_sent=task.payload.get("on_success"),
            )

    def _deliver(
        self,
        chat_id: str,
        text: str,
        *,
        delivery_key: str,
        delivery_type: str,
        task: Task,
        on_sent: Callable[[], None] | None = None,
    ) -> None:
        """函数功能：`BoundedTaskExecutor._deliver` 在类 `BoundedTaskExecutor` 中负责处理 deliver，服务于本文件职责：本地有界异步执行器。
        传参：
            chat_id: chat id 参数，由调用方传入，类型为 `str`。
            text: 输入文本内容，类型为 `str`。
            delivery_key: delivery key 参数，由调用方传入，类型为 `str`。
            delivery_type: delivery type 参数，由调用方传入，类型为 `str`。
            task: task 参数，由调用方传入，类型为 `Task`。
            on_sent: on sent 参数，由调用方传入，类型为 `Callable[[], None] | None`，默认值为 `None`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        if self._send_text is None:
            return
        reserved = reserve_delivery(
            delivery_key,
            delivery_type=delivery_type,
            space_id=task.space_id,
            message_id=task.message_id,
        )
        if reserved is None:
            log_event(
                "runtime.delivery_skipped",
                status="skipped",
                space_id=task.space_id,
                message_id=task.message_id,
                record_id=task.id,
                extra={"delivery_key": delivery_key, "delivery_type": delivery_type},
            )
            return

        try:
            sent = self._send_text(chat_id, text)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if _looks_uncertain_send_error(exc):
                mark_unknown(delivery_key, error)
            else:
                mark_failed(delivery_key, error)
            raise

        if sent is False:
            error = "send_text returned False"
            mark_failed(delivery_key, error)
            raise RuntimeError(error)

        mark_sent(delivery_key)
        if on_sent is not None:
            on_sent()

    def _deliver_query_failure_notice(self, chat_id: str, task: Task) -> None:
        """函数功能：`BoundedTaskExecutor._deliver_query_failure_notice` 在类 `BoundedTaskExecutor` 中负责查询 failure notice，服务于本文件职责：本地有界异步执行器。
        传参：
            chat_id: chat id 参数，由调用方传入，类型为 `str`。
            task: task 参数，由调用方传入，类型为 `Task`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        try:
            self._deliver(
                chat_id,
                "这次查询失败了，可能是模型暂时没有返回内容。请稍后再问一次。",
                delivery_key=f"{task.payload.get('delivery_key')}:failed",
                delivery_type="query_failed",
                task=task,
            )
        except Exception:
            LOGGER.exception("Failed to send query failure notice: task_id=%s", task.id)

    def _summary_lock(self, space_id: str) -> threading.Lock:
        """函数功能：`BoundedTaskExecutor._summary_lock` 在类 `BoundedTaskExecutor` 中负责加锁 summary，服务于本文件职责：本地有界异步执行器。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        返回结果说明：
            返回 `threading.Lock` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        with self._summary_locks_guard:
            lock = self._summary_locks.get(space_id)
            if lock is None:
                lock = threading.Lock()
                self._summary_locks[space_id] = lock
            return lock

    def _reserve_inflight_ingest(self, key: tuple[str, str]) -> bool:
        """函数功能：`BoundedTaskExecutor._reserve_inflight_ingest` 在类 `BoundedTaskExecutor` 中负责预约 inflight ingest，服务于本文件职责：本地有界异步执行器。
        传参：
            key: key 参数，由调用方传入，类型为 `tuple[str, str]`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        with self._inflight_ingest_lock:
            if key in self._inflight_ingest_keys:
                return False
            self._inflight_ingest_keys.add(key)
            return True

    def _release_inflight_ingest(self, key: tuple[str, str]) -> None:
        """函数功能：`BoundedTaskExecutor._release_inflight_ingest` 在类 `BoundedTaskExecutor` 中负责释放 inflight ingest，服务于本文件职责：本地有界异步执行器。
        传参：
            key: key 参数，由调用方传入，类型为 `tuple[str, str]`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        with self._inflight_ingest_lock:
            self._inflight_ingest_keys.discard(key)


def _record_to_dict(record: Any) -> dict[str, Any]:
    """函数功能：`_record_to_dict` 负责记录 to dict，服务于本文件职责：本地有界异步执行器。
    传参：
        record: 待处理或持久化的记录对象，类型为 `Any`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    if isinstance(record, dict):
        return dict(record)
    return asdict(record)


def _note_id(note: Any) -> str:
    """函数功能：`_note_id` 负责处理 note id，服务于本文件职责：本地有界异步执行器。
    传参：
        note: note 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    if note is None:
        return ""
    if isinstance(note, dict):
        return str(note.get("id") or "")
    return str(getattr(note, "id", "") or "")


def _task_timing(task: Task) -> dict[str, int | None]:
    """函数功能：`_task_timing` 负责处理 task timing，服务于本文件职责：本地有界异步执行器。
    传参：
        task: task 参数，由调用方传入，类型为 `Task`。
    返回结果说明：
        返回 `dict[str, int | None]`，表示结构化结果、载荷或状态映射。
    """
    return {
        "queue_wait_ms": task.queue_wait_ms,
        "execution_ms": task.execution_ms,
        "total_duration_ms": task.total_duration_ms,
    }


def _looks_uncertain_send_error(exc: BaseException) -> bool:
    """函数功能：`_looks_uncertain_send_error` 负责发送 error，服务于本文件职责：本地有界异步执行器。
    传参：
        exc: 当前捕获的异常对象，类型为 `BaseException`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    text = f"{type(exc).__name__}: {exc}".casefold()
    return "timeout" in text or "timed out" in text or "connection" in text


_default_executor: BoundedTaskExecutor | None = None
_default_lock = threading.Lock()


def get_task_executor(send_text: SendText | None = None) -> BoundedTaskExecutor:
    """函数功能：`get_task_executor` 负责获取 task executor，服务于本文件职责：本地有界异步执行器。
    传参：
        send_text: send text 参数，由调用方传入，类型为 `SendText | None`，默认值为 `None`。
    返回结果说明：
        返回 `BoundedTaskExecutor` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    global _default_executor
    with _default_lock:
        if _default_executor is None:
            _default_executor = BoundedTaskExecutor(send_text=send_text)
        elif send_text is not None:
            _default_executor.set_send_text(send_text)
        return _default_executor
