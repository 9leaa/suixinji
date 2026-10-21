"""Deterministic calendar tools shared by memory writes and time-range queries."""

from calendar import monthrange
from datetime import date, datetime, timedelta
import os
import re
from zoneinfo import ZoneInfo


def timezone_name(explicit=None):
    return explicit or os.getenv("SUIXINJI_TIMEZONE", "Asia/Shanghai")


def local_time(reference, timezone=None):
    zone = ZoneInfo(timezone_name(timezone))
    if not reference:
        raise ValueError("缺少消息时间，不能计算相对日期")
    value = datetime.fromisoformat(reference) if isinstance(reference, str) else reference
    return value.replace(tzinfo=zone) if value.tzinfo is None else value.astimezone(zone)


def resolve_range(expression, reference, timezone=None):
    """Inclusive local dates. Unsupported/vague expressions return None, never a guessed date."""
    text = re.sub(r"\s+", "", expression or "")
    if not text:
        return None
    day = local_time(reference, timezone).date()
    interval = re.fullmatch(r"(.+?)(?:到|至|～|~)(.+)", text)
    if interval:
        left = resolve_range(interval[1], reference, timezone)
        right = resolve_range(interval[2], reference, timezone)
        if not left or not right:
            return None
        if right["end"] < left["start"]:
            raise ValueError("结束日期早于开始日期")
        return {"start": left["start"], "end": right["end"], "timezone": timezone_name(timezone)}
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", text)
    if dates and text == dates[0]:
        start = end = date.fromisoformat(dates[0])
    elif match := re.fullmatch(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]?", text):
        start = end = date(int(match[1] or day.year), int(match[2]), int(match[3]))
    elif match := re.fullmatch(r"(上上|上|本|这|下|下下)?(?:周|星期)([一二三四五六日天])?", text):
        offset = {"上上": -2, "上": -1, "本": 0, "这": 0, "下": 1, "下下": 2, None: 0}[match[1]]
        start = day - timedelta(days=day.weekday()) + timedelta(weeks=offset)
        end = start + timedelta(days=6)
        if match[2]:
            start += timedelta(days="一二三四五六日".index(match[2].replace("天", "日")))
            end = start
    elif text in {"月底", "月末"}:
        start = end = date(day.year, day.month, monthrange(day.year, day.month)[1])
    elif text in {"前天", "昨天", "今天", "明天", "后天"}:
        start = end = day + timedelta(days={"前天": -2, "昨天": -1, "今天": 0, "明天": 1, "后天": 2}[text])
    elif match := re.fullmatch(r"(上|本|这|下)个?月", text):
        month = day.year * 12 + day.month - 1 + {"上": -1, "本": 0, "这": 0, "下": 1}[match[1]]
        year, index = divmod(month, 12)
        start = date(year, index + 1, 1)
        end = date(year, index + 1, monthrange(year, index + 1)[1])
    elif match := re.fullmatch(r"(?:最近|近)([一二三四五六七八九十]|\d+)天", text):
        count = int(match[1]) if match[1].isdigit() else "一二三四五六七八九十".index(match[1]) + 1
        if count < 1:
            return None
        start, end = day - timedelta(days=count - 1), day
    else:
        return None
    if end < start:
        raise ValueError("结束日期早于开始日期")
    return {"start": start.isoformat(), "end": end.isoformat(), "timezone": timezone_name(timezone)}


def resolve_due(expression, reference, timezone=None):
    text = re.sub(r"(?:之前|以前|前|截止)$", "", (expression or "").strip())
    clock = re.fullmatch(r"(.+?)(?:(上午|下午|晚上))?(\d{1,2})(?::(\d{2})|点(?:(\d{1,2})分?)?)", text)
    period = resolve_range(clock[1] if clock else text, reference, timezone)
    if not period:
        return None
    if clock:
        hour = int(clock[3])
        if clock[2] in {"下午", "晚上"} and hour < 12:
            hour += 12
        minute = int(clock[4] or clock[5] or 0)
        return (
            datetime.combine(date.fromisoformat(period["end"]), datetime.min.time())
            .replace(hour=hour, minute=minute, tzinfo=ZoneInfo(period["timezone"]))
            .isoformat()
        )
    return period["end"]  # Date precision: never manufacture 23:59:59.


def task_time_fields(value, note):
    reference = note.get("ts") or note.get("created_at")
    zone = timezone_name(note.get("timezone"))
    period_text, due_text = value.period_expression, value.due_expression
    for text in (period_text, due_text):
        if text and text not in note["text"]:
            raise ValueError("时间表达必须来自当前消息原文")
    period = resolve_range(period_text, reference, zone) if period_text else None
    due_reference = (
        period["start"]
        if period and "月" in (period_text or "") and re.fullmatch(r"(?:月底|月末)(?:前|之前)?", due_text or "")
        else reference
    )
    due = resolve_due(due_text, due_reference, zone) if due_text else None
    return {
        "period_start": period["start"] if period else None,
        "period_end": period["end"] if period else None,
        "due_at": due,
        "time_expression": {"period": period_text, "due": due_text},
        "timezone": zone,
        "unresolved": [text for text, result in ((period_text, period), (due_text, due)) if text and not result],
    }
