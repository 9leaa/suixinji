"""文件作用：consolidation scheduler run/lease。

项目关系：本文件依赖 `memory`、`memory.repository`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from datetime import date, datetime, timedelta

from memory import scheduler
from memory.repository import (
    _connect,
    consolidation_period_key,
    get_consolidation_run,
    mark_consolidation_completed,
    mark_consolidation_failed,
    reserve_consolidation_run,
)


def test_consolidation_period_key_formats():
    """函数功能：`test_consolidation_period_key_formats` 负责验证 consolidation period key formats 场景，服务于本文件职责：consolidation scheduler run/lease。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    day = date(2026, 7, 14)

    assert consolidation_period_key("daily", day) == "2026-07-14"
    assert consolidation_period_key("weekly", day) == "2026-W29"
    assert consolidation_period_key("monthly", day) == "2026-07"


def test_reserve_completed_running_failed_and_stale_runs():
    """函数功能：`test_reserve_completed_running_failed_and_stale_runs` 负责验证 reserve completed running failed and stale runs 场景，服务于本文件职责：consolidation scheduler run/lease。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    period = "2026-07-14"
    run = reserve_consolidation_run("space-1", "daily", period)
    assert run is not None

    assert reserve_consolidation_run("space-1", "daily", period) is None

    mark_consolidation_failed(run.id, "boom")
    retry = reserve_consolidation_run("space-1", "daily", period)
    assert retry is not None
    assert retry.id != run.id

    mark_consolidation_completed(retry.id, {"ok": True})
    assert reserve_consolidation_run("space-1", "daily", period) is None

    other_period = "2026-07-15"
    stale = reserve_consolidation_run("space-1", "daily", other_period)
    assert stale is not None
    old = (datetime.now().astimezone() - timedelta(minutes=30)).isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute("UPDATE memory_consolidation_runs SET started_at = ? WHERE id = ?", (old, stale.id))
    stale_retry = reserve_consolidation_run("space-1", "daily", other_period)
    assert stale_retry is not None
    assert stale_retry.id != stale.id


def test_scheduler_run_once_is_db_idempotent(monkeypatch):
    """函数功能：`test_scheduler_run_once_is_db_idempotent` 负责验证 scheduler run once is db idempotent 场景，服务于本文件职责：consolidation scheduler run/lease。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    calls = []
    monkeypatch.setattr(scheduler, "list_memory_space_ids", lambda: ["space-1", "space-2"])
    monkeypatch.setattr(
        scheduler,
        "run_memory_consolidation",
        lambda space_id, cadence: calls.append((space_id, cadence)) or {"space_id": space_id, "processed_count": 0},
    )

    first = scheduler.run_memory_consolidation_once("daily", today=date(2026, 7, 14))
    second = scheduler.run_memory_consolidation_once("daily", today=date(2026, 7, 14))
    next_day = scheduler.run_memory_consolidation_once("daily", today=date(2026, 7, 15))

    assert [item["status"] for item in first["results"]] == ["completed", "completed"]
    assert [item["status"] for item in second["results"]] == ["skipped", "skipped"]
    assert [item["status"] for item in next_day["results"]] == ["completed", "completed"]
    assert calls == [
        ("space-1", "daily"),
        ("space-2", "daily"),
        ("space-1", "daily"),
        ("space-2", "daily"),
    ]


def test_scheduler_failed_run_can_retry(monkeypatch):
    """函数功能：`test_scheduler_failed_run_can_retry` 负责验证 scheduler failed run can retry 场景，服务于本文件职责：consolidation scheduler run/lease。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    attempts = {"count": 0}

    def flaky(space_id, cadence):
        """函数功能：`flaky` 负责处理 flaky，服务于本文件职责：consolidation scheduler run/lease。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据。
            cadence: cadence 参数，由调用方传入。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary")
        return {"space_id": space_id}

    monkeypatch.setattr(scheduler, "run_memory_consolidation", flaky)

    first = scheduler.run_memory_consolidation_once("weekly", space_ids=["space-1"], today=date(2026, 7, 20))
    second = scheduler.run_memory_consolidation_once("weekly", space_ids=["space-1"], today=date(2026, 7, 20))

    assert first["results"][0]["status"] == "failed"
    assert second["results"][0]["status"] == "completed"
    assert attempts["count"] == 2
    assert get_consolidation_run(second["results"][0]["run_id"]).status == "completed"


def test_scheduler_marks_partial_daily_result_as_failed(monkeypatch):
    """函数功能：`test_scheduler_marks_partial_daily_result_as_failed` 负责验证 scheduler marks partial daily result as failed 场景，服务于本文件职责：consolidation scheduler run/lease。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(
        scheduler,
        "run_memory_consolidation",
        lambda space_id, cadence: {
            "space_id": space_id,
            "processed_count": 2,
            "failed_count": 1,
            "status": "partial",
        },
    )

    report = scheduler.run_memory_consolidation_once("daily", space_ids=["space-1"], today=date(2026, 7, 14))
    result = report["results"][0]

    assert result["status"] == "failed"
    assert result["error"] == "1 notes failed"
    assert get_consolidation_run(result["run_id"]).status == "failed"
