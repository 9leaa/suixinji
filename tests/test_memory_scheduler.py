from datetime import date

from memory import scheduler


def test_due_cadences_daily_weekly_monthly():
    due = scheduler.due_cadences(date(2026, 6, 1), {})

    assert due == ["daily", "weekly", "monthly"]


def test_scheduler_tick_runs_due_cadences_once(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "run_memory_consolidation_once", lambda cadence, today=None: calls.append((cadence, today)) or {"cadence": cadence})
    state = {}

    first = scheduler.run_memory_scheduler_tick(state, today=date(2026, 6, 1))
    second = scheduler.run_memory_scheduler_tick(state, today=date(2026, 6, 1))

    assert first["ran"] == ["daily", "weekly", "monthly"]
    assert second["ran"] == []
    assert calls == [("daily", date(2026, 6, 1)), ("weekly", date(2026, 6, 1)), ("monthly", date(2026, 6, 1))]
