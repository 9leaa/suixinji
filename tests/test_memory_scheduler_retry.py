"""文件作用：memory scheduler 失败重试和 lease。

项目关系：本文件依赖 `memory`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from datetime import date

from memory import scheduler


def test_scheduler_tick_retries_same_day_after_failure(monkeypatch):
    """函数功能：`test_scheduler_tick_retries_same_day_after_failure` 负责验证 scheduler tick retries same day after failure 场景，服务于本文件职责：memory scheduler 失败重试和 lease。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    attempts = {"count": 0}

    def run_once(cadence, today=None):
        """函数功能：`run_once` 负责运行 once，服务于本文件职责：memory scheduler 失败重试和 lease。
        传参：
            cadence: cadence 参数，由调用方传入。
            today: today 参数，由调用方传入，默认值为 `None`。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
        attempts["count"] += 1
        status = "failed" if attempts["count"] == 1 else "completed"
        return {"cadence": cadence, "results": [{"space_id": "space-1", "status": status}]}

    monkeypatch.setattr(scheduler, "run_memory_consolidation_once", run_once)
    state = {}

    first = scheduler.run_memory_scheduler_tick(state, today=date(2026, 7, 14))
    second = scheduler.run_memory_scheduler_tick(state, today=date(2026, 7, 14))
    third = scheduler.run_memory_scheduler_tick(state, today=date(2026, 7, 14))

    assert first["ran"] == ["daily"]
    assert second["ran"] == ["daily"]
    assert third["ran"] == []
    assert state["daily"] == "2026-07-14"
    assert attempts["count"] == 2


def test_scheduler_retries_only_failed_space_same_day(monkeypatch):
    """函数功能：`test_scheduler_retries_only_failed_space_same_day` 负责验证 scheduler retries only failed space same day 场景，服务于本文件职责：memory scheduler 失败重试和 lease。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    attempts = {"space-a": 0, "space-b": 0}
    monkeypatch.setattr(scheduler, "list_memory_space_ids", lambda: ["space-a", "space-b"])

    def run_consolidation(space_id, cadence):
        """函数功能：`run_consolidation` 负责运行 consolidation，服务于本文件职责：memory scheduler 失败重试和 lease。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据。
            cadence: cadence 参数，由调用方传入。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
        attempts[space_id] += 1
        if space_id == "space-b" and attempts[space_id] == 1:
            raise RuntimeError("temporary")
        return {"space_id": space_id, "processed_count": 0}

    monkeypatch.setattr(scheduler, "run_memory_consolidation", run_consolidation)
    state = {}

    first = scheduler.run_memory_scheduler_tick(state, today=date(2026, 7, 14))
    second = scheduler.run_memory_scheduler_tick(state, today=date(2026, 7, 14))

    assert [item["status"] for item in first["reports"][0]["results"]] == ["completed", "failed"]
    assert [item["status"] for item in second["reports"][0]["results"]] == ["skipped", "completed"]
    assert attempts == {"space-a": 1, "space-b": 2}
    assert state["daily"] == "2026-07-14"
