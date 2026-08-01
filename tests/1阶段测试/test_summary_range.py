"""文件作用：总结时间范围解析。

项目关系：本文件依赖 `summary`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from datetime import datetime, timezone, timedelta

import pytest

from summary import daily_summary

TZ = timezone(timedelta(hours=8))


def test_parse_summary_range_aliases():
    """函数功能：`test_parse_summary_range_aliases` 负责验证 parse summary range aliases 场景，服务于本文件职责：总结时间范围解析。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`test_build_time_range` 负责验证 build time range 场景，服务于本文件职责：总结时间范围解析。
    传参：
        range_key: range key 参数，由调用方传入。
        start: start 参数，由调用方传入。
        end: end 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    now = datetime(2026, 6, 7, 15, 30, tzinfo=TZ)

    got_start, got_end = daily_summary.build_time_range(range_key, now)

    assert got_start.isoformat() == start
    assert got_end.isoformat() == end


def test_build_time_range_rejects_unknown_range():
    """函数功能：`test_build_time_range_rejects_unknown_range` 负责验证 build time range rejects unknown range 场景，服务于本文件职责：总结时间范围解析。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    with pytest.raises(ValueError):
        daily_summary.build_time_range("bad")


def test_load_notes_in_range_filters_and_sorts(monkeypatch):
    """函数功能：`test_load_notes_in_range_filters_and_sorts` 负责验证 load notes in range filters and sorts 场景，服务于本文件职责：总结时间范围解析。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
