"""文件作用：Memory 定时作业。

项目关系：本文件依赖 `core.file_lock`、`memory.consolidator`、`memory.expiry`、`memory.repository` 等 5 个模块；被 `apps.handlers`、`apps.scheduler`、`bot.feishu_bot`、`memory.service`。
"""



from __future__ import annotations

import logging
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

from core.file_lock import safe_space_id
from memory.consolidator import merge_duplicate_episodic, process_unextracted_notes, run_monthly_semantic_consolidation
from memory.expiry import run_expiry_once
from memory.repository import (
    consolidation_period_key,
    flush_access_counts,
    mark_consolidation_completed,
    mark_consolidation_failed,
    reserve_consolidation_run,
)
from storage.note_storage import NOTES_DIR, list_note_space_ids

LOGGER = logging.getLogger(__name__)
DEFAULT_MEMORY_SCHEDULER_INTERVAL_SECONDS = 3600
_EVALUATION_SPACE_PREFIXES = ("l1_eval_", "l2_eval_", "l3_eval_")


def is_evaluation_space(space_id: str) -> bool:
    """Return whether a space is synthetic evaluator state, never user data."""
    return str(space_id or "").startswith(_EVALUATION_SPACE_PREFIXES)


def list_memory_space_ids(notes_dir: Path | None = None) -> list[str]:
    """函数功能：`list_memory_space_ids` 负责列出 memory space ids，服务于本文件职责：Memory 定时作业。
    传参：
        notes_dir: notes dir 参数，由调用方传入，类型为 `Path | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
    if notes_dir is None:
        return list_note_space_ids()
    root = notes_dir or NOTES_DIR
    if not root.exists():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir())


def run_memory_consolidation(space_id: str, cadence: str) -> dict[str, Any]:
    """函数功能：`run_memory_consolidation` 负责运行 memory consolidation，服务于本文件职责：Memory 定时作业。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        cadence: cadence 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    cadence = cadence.strip().lower()
    if cadence == "daily":
        return {**process_unextracted_notes(space_id), "expired_count": run_expiry_once(space_id=space_id)["expired_count"]}
    if cadence == "weekly":
        return merge_duplicate_episodic(space_id)
    if cadence == "monthly":
        return run_monthly_semantic_consolidation(space_id)
    raise ValueError(f"unknown memory consolidation cadence: {cadence}")


def run_memory_consolidation_once(cadence: str, *, space_ids: list[str] | None = None, today: date | None = None) -> dict[str, Any]:
    """函数功能：`run_memory_consolidation_once` 负责运行 memory consolidation once，服务于本文件职责：Memory 定时作业。
    传参：
        cadence: cadence 参数，由调用方传入，类型为 `str`。
        space_ids: space ids 参数，由调用方传入，类型为 `list[str] | None`，默认值为 `None`。
        today: today 参数，由调用方传入，类型为 `date | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    current_day = today or date.today()
    period_key = consolidation_period_key(cadence, current_day)
    targets = space_ids or list_memory_space_ids()
    results = []
    for space_id in targets:
        safe_id = safe_space_id(space_id)
        # Evaluation runners seed real Notes and Memories so retrieval is
        # realistic. They are intentionally read-only after seeding: a
        # background consolidation pass must never extract their source Notes
        # a second time or enqueue follow-up work into the shared workers.
        if is_evaluation_space(safe_id):
            results.append(
                {
                    "space_id": safe_id,
                    "cadence": cadence,
                    "period_key": period_key,
                    "status": "skipped",
                    "reason": "synthetic_evaluation_space",
                }
            )
            continue
        run = reserve_consolidation_run(safe_id, cadence, period_key)
        if run is None:
            LOGGER.info(
                "memory.consolidation.skipped space_id=%s cadence=%s period_key=%s",
                safe_id,
                cadence,
                period_key,
            )
            results.append(
                {
                    "space_id": safe_id,
                    "cadence": cadence,
                    "period_key": period_key,
                    "status": "skipped",
                    "reason": "already_reserved_or_completed",
                }
            )
            continue
        started = time.monotonic()
        LOGGER.info(
            "memory.consolidation.reserve space_id=%s cadence=%s period_key=%s run_id=%s status=%s",
            safe_id,
            cadence,
            period_key,
            run.id,
            run.status,
        )
        try:
            result = run_memory_consolidation(safe_id, cadence)
            if result.get("status") == "partial":
                error = f"{result.get('failed_count', 0)} notes failed"
                mark_consolidation_failed(run.id, error)
                LOGGER.warning(
                    "memory.consolidation.failed space_id=%s cadence=%s period_key=%s run_id=%s duration_ms=%s error=%s",
                    safe_id,
                    cadence,
                    period_key,
                    run.id,
                    int((time.monotonic() - started) * 1000),
                    error,
                )
                results.append(
                    {
                        **result,
                        "cadence": cadence,
                        "period_key": period_key,
                        "run_id": run.id,
                        "status": "failed",
                        "error": error,
                    }
                )
                continue
            result = {**result, "cadence": cadence, "period_key": period_key, "run_id": run.id, "status": result.get("status", "completed")}
            mark_consolidation_completed(run.id, result)
            LOGGER.info(
                "memory.consolidation.completed space_id=%s cadence=%s period_key=%s run_id=%s duration_ms=%s",
                safe_id,
                cadence,
                period_key,
                run.id,
                int((time.monotonic() - started) * 1000),
            )
            results.append(result)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            mark_consolidation_failed(run.id, error)
            LOGGER.exception(
                "memory.consolidation.failed space_id=%s cadence=%s period_key=%s run_id=%s duration_ms=%s",
                safe_id,
                cadence,
                period_key,
                run.id,
                int((time.monotonic() - started) * 1000),
            )
            results.append({"space_id": safe_id, "cadence": cadence, "period_key": period_key, "run_id": run.id, "status": "failed", "error": str(exc)})
    return {"cadence": cadence, "period_key": period_key, "space_count": len(targets), "results": results}


def _report_has_failures(report: dict[str, Any]) -> bool:
    """函数功能：`_report_has_failures` 负责判断是否包含 failures，服务于本文件职责：Memory 定时作业。
    传参：
        report: report 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    return any(item.get("status") == "failed" for item in report.get("results", []))


def _report_is_complete(report: dict[str, Any]) -> bool:
    """函数功能：`_report_is_complete` 负责判断是否为 complete，服务于本文件职责：Memory 定时作业。
    传参：
        report: report 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    results = report.get("results", [])
    if not results:
        return True
    return not _report_has_failures(report)


def due_cadences(today: date, last_run_dates: dict[str, str]) -> list[str]:
    """函数功能：`due_cadences` 负责处理 due cadences，服务于本文件职责：Memory 定时作业。
    传参：
        today: today 参数，由调用方传入，类型为 `date`。
        last_run_dates: last run dates 参数，由调用方传入，类型为 `dict[str, str]`。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
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
    """函数功能：`run_memory_scheduler_tick` 负责运行 memory scheduler tick，服务于本文件职责：Memory 定时作业。
    传参：
        last_run_dates: last run dates 参数，由调用方传入，类型为 `dict[str, str] | None`，默认值为 `None`。
        today: today 参数，由调用方传入，类型为 `date | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    state = last_run_dates if last_run_dates is not None else {}
    current_day = today or date.today()
    reports = []
    for cadence in due_cadences(current_day, state):
        report = run_memory_consolidation_once(cadence, today=current_day)
        reports.append(report)
        if _report_is_complete(report):
            state[cadence] = current_day.isoformat()
        else:
            LOGGER.warning(
                "Memory consolidation cadence remains retryable: cadence=%s date=%s",
                cadence,
                current_day.isoformat(),
            )
    try:
        flushed_access_counts = flush_access_counts()
    except Exception:
        LOGGER.warning("Memory access counter flush failed", exc_info=True)
        flushed_access_counts = 0
    return {
        "date": current_day.isoformat(),
        "ran": [report["cadence"] for report in reports],
        "reports": reports,
        "flushed_access_counts": flushed_access_counts,
    }


def start_memory_scheduler(interval_seconds: int = DEFAULT_MEMORY_SCHEDULER_INTERVAL_SECONDS) -> threading.Thread:
    """函数功能：`start_memory_scheduler` 负责启动 memory scheduler，服务于本文件职责：Memory 定时作业。
    传参：
        interval_seconds: interval seconds 参数，由调用方传入，类型为 `int`，默认值为 `DEFAULT_MEMORY_SCHEDULER_INTERVAL_SECONDS`。
    返回结果说明：
        返回 `threading.Thread` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    last_run_dates: dict[str, str] = {}

    def _loop() -> None:
        """函数功能：`_loop` 负责处理 loop，服务于本文件职责：Memory 定时作业。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        while True:
            try:
                run_memory_scheduler_tick(last_run_dates)
            except Exception:
                LOGGER.exception("Memory scheduler tick failed")
            time.sleep(max(60, int(interval_seconds)))

    thread = threading.Thread(target=_loop, name="memory-scheduler", daemon=True)
    thread.start()
    return thread
