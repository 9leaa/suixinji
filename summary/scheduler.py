"""P4 automatic summary scheduler."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime

from core.observability import log_event
from runtime.delivery_store import auto_summary_key
from runtime.executor import BoundedTaskExecutor
from runtime.task import TASK_REJECTED
from summary.reconciliation import reconcile_auto_summary_delivery
from summary.subscription import (
    SummarySubscription,
    list_enabled_summary_subscriptions,
    mark_summary_sent,
)

LOGGER = logging.getLogger(__name__)
_started = False
_started_lock = threading.Lock()


def _minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _is_due(sub: SummarySubscription, now: datetime) -> bool:
    today = now.date().isoformat()
    if sub.last_sent_date == today:
        return False
    now_minutes = now.hour * 60 + now.minute
    return now_minutes >= _minutes(sub.time)


def run_summary_scheduler_once(
    send_text: Callable[[str, str], bool],
    executor: BoundedTaskExecutor | None = None,
    *,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now().astimezone()
    today = now.date().isoformat()
    count = 0
    tick_start = time.perf_counter()
    subscriptions = list_enabled_summary_subscriptions()

    log_event(
        "summary.scheduler.tick",
        status="start",
        extra={"subscription_count": len(subscriptions)},
    )

    for sub in subscriptions:
        trigger_start = time.perf_counter()
        ctx = {"space_id": sub.space_id}
        range_key = sub.range_key
        try:
            if reconcile_auto_summary_delivery(sub.space_id, range_key, today):
                continue

            if not _is_due(sub, now):
                continue

            log_event(
                "summary.auto.trigger",
                status="start",
                **ctx,
                extra={"range_key": range_key, "time": sub.time},
            )

            if executor is None:
                from runtime.executor import get_task_executor

                executor = get_task_executor(send_text)

            def on_success(
                space_id: str = sub.space_id,
                sent_date: str = today,
                success_ctx: dict[str, str] = dict(ctx),
                success_range_key: str = range_key,
            ) -> None:
                mark_summary_sent(space_id, sent_date)
                log_event(
                    "summary.auto.send",
                    status="success",
                    **success_ctx,
                    extra={"range_key": success_range_key},
                )

            task = executor.submit_summary(
                sub.space_id,
                range_key,
                sub.chat_id,
                on_success=on_success,
                delivery_key=auto_summary_key(sub.space_id, range_key, today),
                delivery_type="auto_summary",
            )
            if task.status != TASK_REJECTED:
                count += 1
                log_event(
                    "summary.auto.trigger",
                    status="success",
                    duration_ms=int((time.perf_counter() - trigger_start) * 1000),
                    **ctx,
                    extra={"range_key": range_key, "task_id": task.id},
                )
            else:
                error = task.error or "summary task rejected"
                log_event(
                    "summary.auto.trigger",
                    level="error",
                    status="rejected",
                    duration_ms=int((time.perf_counter() - trigger_start) * 1000),
                    error=error,
                    **ctx,
                    extra={"range_key": range_key, "task_id": task.id},
                )
        except Exception as exc:
            LOGGER.exception("Failed to process summary subscription: space_id=%s", sub.space_id)
            log_event(
                "summary.scheduler.subscription",
                level="error",
                status="failed",
                duration_ms=int((time.perf_counter() - trigger_start) * 1000),
                error=f"{type(exc).__name__}: {exc}",
                space_id=sub.space_id,
                extra={"range_key": range_key, "time": sub.time},
            )
            continue

    log_event(
        "summary.scheduler.tick",
        status="success",
        duration_ms=int((time.perf_counter() - tick_start) * 1000),
        extra={"subscription_count": len(subscriptions), "sent_count": count},
    )
    return count


def run_scheduler_tick_safely(
    send_text: Callable[[str, str], bool],
    executor: BoundedTaskExecutor | None = None,
) -> None:
    tick_started = time.perf_counter()
    try:
        run_summary_scheduler_once(send_text, executor=executor)
    except Exception as exc:
        LOGGER.exception("Summary scheduler tick failed")
        try:
            log_event(
                "summary.scheduler.tick",
                level="error",
                status="failed",
                duration_ms=int((time.perf_counter() - tick_started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            LOGGER.exception("Failed to write scheduler failure log")


def start_summary_scheduler(
    send_text: Callable[[str, str], bool],
    *,
    interval_seconds: int = 60,
    executor: BoundedTaskExecutor | None = None,
) -> None:
    global _started
    with _started_lock:
        if _started:
            return
        _started = True

    def loop() -> None:
        LOGGER.info("P4 summary scheduler started")
        while True:
            try:
                run_scheduler_tick_safely(send_text, executor=executor)
            finally:
                time.sleep(interval_seconds)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
