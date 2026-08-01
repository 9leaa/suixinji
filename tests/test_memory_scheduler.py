"""文件作用：过期/合并/向量定时任务。

项目关系：本文件依赖 `memory`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from datetime import date

from memory import scheduler


def test_due_cadences_daily_weekly_monthly():
    """函数功能：`test_due_cadences_daily_weekly_monthly` 负责验证 due cadences daily weekly monthly 场景，服务于本文件职责：过期/合并/向量定时任务。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    due = scheduler.due_cadences(date(2026, 6, 1), {})

    assert due == ["daily", "weekly", "monthly"]


def test_scheduler_tick_runs_due_cadences_once(monkeypatch):
    """函数功能：`test_scheduler_tick_runs_due_cadences_once` 负责验证 scheduler tick runs due cadences once 场景，服务于本文件职责：过期/合并/向量定时任务。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    calls = []
    monkeypatch.setattr(scheduler, "run_memory_consolidation_once", lambda cadence, today=None: calls.append((cadence, today)) or {"cadence": cadence})
    state = {}

    first = scheduler.run_memory_scheduler_tick(state, today=date(2026, 6, 1))
    second = scheduler.run_memory_scheduler_tick(state, today=date(2026, 6, 1))

    assert first["ran"] == ["daily", "weekly", "monthly"]
    assert second["ran"] == []
    assert calls == [("daily", date(2026, 6, 1)), ("weekly", date(2026, 6, 1)), ("monthly", date(2026, 6, 1))]
