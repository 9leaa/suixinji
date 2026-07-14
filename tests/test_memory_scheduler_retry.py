from datetime import date

from memory import scheduler


def test_scheduler_tick_retries_same_day_after_failure(monkeypatch):
    attempts = {"count": 0}

    def run_once(cadence, today=None):
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
    attempts = {"space-a": 0, "space-b": 0}
    monkeypatch.setattr(scheduler, "list_memory_space_ids", lambda: ["space-a", "space-b"])

    def run_consolidation(space_id, cadence):
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
