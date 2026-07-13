"""P4 automatic summary scheduler."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime

from core.observability import log_event
from summary.daily_summary import generate_summary
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


def run_summary_scheduler_once(send_text: Callable[[str, str], bool]) -> int:
    now = datetime.now().astimezone()
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
        if not _is_due(sub, now):
            continue

        trigger_start = time.perf_counter()
        ctx = {"space_id": sub.space_id}
        log_event(
            "summary.auto.trigger",
            status="start",
            **ctx,
            extra={"range_key": sub.range_key, "time": sub.time},
        )

        try:
            result = generate_summary(sub.space_id, sub.range_key)
            send_start = time.perf_counter()
            try:
                sent = send_text(sub.chat_id, result.markdown)
            except Exception as exc:
                log_event(
                    "summary.auto.send",
                    level="error",
                    status="failed",
                    duration_ms=int((time.perf_counter() - send_start) * 1000),
                    error=f"{type(exc).__name__}: {exc}",
                    **ctx,
                    extra={"range_key": sub.range_key},
                )
                raise

            if sent is not False:
                log_event(
                    "summary.auto.send",
                    status="success",
                    duration_ms=int((time.perf_counter() - send_start) * 1000),
                    **ctx,
                    extra={"range_key": sub.range_key, "markdown_len": len(result.markdown)},
                )
                mark_summary_sent(sub.space_id, today)
                count += 1
                log_event(
                    "summary.auto.trigger",
                    status="success",
                    duration_ms=int((time.perf_counter() - trigger_start) * 1000),
                    **ctx,
                    extra={"range_key": sub.range_key},
                )
            else:
                error = "send_text returned False"
                log_event(
                    "summary.auto.send",
                    level="error",
                    status="failed",
                    duration_ms=int((time.perf_counter() - send_start) * 1000),
                    error=error,
                    **ctx,
                    extra={"range_key": sub.range_key},
                )
                log_event(
                    "summary.auto.trigger",
                    level="error",
                    status="failed",
                    duration_ms=int((time.perf_counter() - trigger_start) * 1000),
                    error=error,
                    **ctx,
                    extra={"range_key": sub.range_key},
                )
        except Exception as exc:
            LOGGER.exception("Failed to run auto summary: space_id=%s", sub.space_id)
            log_event(
                "summary.auto.trigger",
                level="error",
                status="failed",
                duration_ms=int((time.perf_counter() - trigger_start) * 1000),
                error=f"{type(exc).__name__}: {exc}",
                **ctx,
                extra={"range_key": sub.range_key},
            )

    log_event(
        "summary.scheduler.tick",
        status="success",
        duration_ms=int((time.perf_counter() - tick_start) * 1000),
        extra={"subscription_count": len(subscriptions), "sent_count": count},
    )
    return count


def start_summary_scheduler(
    send_text: Callable[[str, str], bool],
    *,
    interval_seconds: int = 60,
) -> None:
    global _started
    with _started_lock:
        if _started:
            return
        _started = True

    def loop() -> None:
        LOGGER.info("P4 summary scheduler started")
        while True:
            run_summary_scheduler_once(send_text)
            time.sleep(interval_seconds)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()