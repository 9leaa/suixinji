from datetime import datetime, timezone, timedelta

import pytest

from summary import daily_summary

TZ = timezone(timedelta(hours=8))


def test_parse_summary_range_aliases():
    assert daily_summary.parse_summary_range("今天") == "today"
    assert daily_summary.parse_summary_range(" 昨天 ") == "yesterday"
    assert daily_summary.parse_summary_range("7天") == "week"
    assert daily_summary.parse_summary_range("一个月") == "month"
    assert daily_summary.parse_summary_range("半年") == "half_year"
    assert daily_summary.parse_summary_range("一年") == "year"
    assert daily_summary.parse_summary_range("未知") is None


@pytest.mark.parametrize(
    ("range_key", "start", "end"),
    [
        ("today", "2026-06-07T00:00:00+08:00", "2026-06-08T00:00:00+08:00"),
        ("yesterday", "2026-06-06T00:00:00+08:00", "2026-06-07T00:00:00+08:00"),
        ("week", "2026-06-01T00:00:00+08:00", "2026-06-08T00:00:00+08:00"),
        ("month", "2026-05-09T00:00:00+08:00", "2026-06-08T00:00:00+08:00"),
        ("half_year", "2025-12-07T00:00:00+08:00", "2026-06-08T00:00:00+08:00"),
        ("year", "2025-06-08T00:00:00+08:00", "2026-06-08T00:00:00+08:00"),
    ],
)
def test_build_time_range(range_key, start, end):
    now = datetime(2026, 6, 7, 15, 30, tzinfo=TZ)

    got_start, got_end = daily_summary.build_time_range(range_key, now)

    assert got_start.isoformat() == start
    assert got_end.isoformat() == end


def test_build_time_range_rejects_unknown_range():
    with pytest.raises(ValueError):
        daily_summary.build_time_range("bad")


def test_load_notes_in_range_filters_and_sorts(monkeypatch):
    notes = [
        {"id": "outside", "ts": "2026-06-05T23:59:59+08:00"},
        {"id": "later", "ts": "2026-06-06T10:00:00+08:00"},
        {"id": "earlier", "ts": "2026-06-06T08:00:00+08:00"},
        {"id": "end_boundary", "ts": "2026-06-07T00:00:00+08:00"},
        {"id": "invalid", "ts": "not-a-date"},
    ]
    monkeypatch.setattr(daily_summary, "load_index", lambda space_id: notes)

    start = datetime(2026, 6, 6, 0, 0, tzinfo=TZ)
    end = datetime(2026, 6, 7, 0, 0, tzinfo=TZ)

    result = daily_summary.load_notes_in_range("space", start, end)

    assert [note["id"] for note in result] == ["earlier", "later"]
