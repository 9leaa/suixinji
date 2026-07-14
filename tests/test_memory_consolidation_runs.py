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
    day = date(2026, 7, 14)

    assert consolidation_period_key("daily", day) == "2026-07-14"
    assert consolidation_period_key("weekly", day) == "2026-W29"
    assert consolidation_period_key("monthly", day) == "2026-07"


def test_reserve_completed_running_failed_and_stale_runs():
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
    attempts = {"count": 0}

    def flaky(space_id, cadence):
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
