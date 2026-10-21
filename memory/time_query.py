"""Exhaustive time-filtered inventory queries; no vector top-k truncation."""

from datetime import datetime
import re
from core.time_tools import local_time, resolve_range, timezone_name

# Prefer the longest expression, including an explicit interval.
EXPRESSION = r"(?:(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]?(?:到|至|～|~)(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]?|(?:\d{4}-\d{2}-\d{2}(?:(?:到|至|～|~)\d{4}-\d{2}-\d{2})?|(?:上上|下下|上|下|本|这)(?:周|星期)[一二三四五六日天]?|(?:上|本|这|下)个?月|前天|昨天|今天|明天|后天|(?:最近|近)(?:[一二三四五六七八九十]|\d+)天|(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]?))"


def parse_time_query(text):
    if not re.search(r"哪些|列出|列举|查一下|查一查|记录了什么|有什么|的内容", text):
        return None
    match = re.search(EXPRESSION, text)
    if not match:
        return None
    # Only inventory requests are intercepted; a named-task question stays in normal Ask.
    remaining = text[: match.start()] + text[match.end() :]
    kind = "due" if re.search(r"到期|截止", remaining) else "recorded" if re.search(r"记录|记的|消息|内容", remaining) else "period"
    if "任务" not in remaining and kind != "recorded":
        return None
    # Don't turn a topic-specific query into an unfiltered full inventory.
    rest = re.sub(
        r"未完成|尚未完成|已完成|任务|到期|截止|记录了什么|记录|记的|消息|内容|哪些|列出|列举|查一下|查一查|有什么|全部|所有|请|帮|我|的|在|从|有|[？?。，,\s]",
        "",
        remaining,
    )
    if rest:
        return None
    status = "todo" if re.search(r"未完成|尚未完成", remaining) else "done" if "已完成" in remaining else None
    return {"expression": match[0], "kind": kind, "tasks": "任务" in remaining, "status": status}


def answer_time_query(space_id, query, *, owner="owner", access_context=None, now=None, timezone=None, session=None):
    from memory import task_cards
    from memory.task_commands import _allowed
    from storage.note_storage import load_queryable_index

    zone = timezone_name(timezone)
    try:
        span = resolve_range(query["expression"], now or datetime.now().astimezone(), zone)
    except ValueError:
        return "日期范围无效，请检查起止日期。"
    if not span:
        return "暂时无法确定查询日期范围，请补充明确的起止日期。"
    start, end = span["start"], span["end"]

    def within(ts):
        return bool(ts) and start <= local_time(ts, zone).date().isoformat() <= end

    rows = []
    selected = []
    unknown = 0
    if query["tasks"]:
        for record in task_cards.records(space_id, owner):
            if not _allowed(record, space_id, owner, access_context) or query["status"] and record.task_status != query["status"]:
                continue
            card = task_cards.project(record)
            if query["kind"] == "due":
                field = card.get("due_at")
                matched = within(field)
                unknown += not bool(field)
            elif query["kind"] == "period":
                a, b = card.get("period_start"), card.get("period_end")
                matched = bool(a and b and a <= end and b >= start)
                unknown += not bool(a and b)
            else:
                matched = any(within(s.get("message_time")) for s in card["sources"])
            if matched:
                selected.append(record)
                rows.append(
                    f"{card['task']}（{card.get('instance') or '未指定轮次'}） — {record.task_status}"
                    + (f"；截止 {card['due_at']}" if card.get("due_at") else "")
                )
    else:
        for note in load_queryable_index(space_id):
            if str(note.get("user_id") or "owner") != owner or note.get("status") in {"deleted", "forgotten"}:
                continue
            from memory.access import access_decision

            if not access_decision(
                {"owner_id": owner, "access_scope": note.get("access_scope", "owner"), "sensitivity": note.get("sensitivity")},
                access_context or {"requester": owner},
            )[0]:
                continue
            if within(note.get("ts") or note.get("created_at")):
                rows.append(f"{note.get('ts') or note.get('created_at')}：{note['text']}")
    if query["tasks"] and session:
        from memory.task_commands import _snapshot

        _snapshot(space_id, session, selected)
    label = {"due": "截止时间", "period": "任务所属周期（与范围重叠）", "recorded": "消息记录时间"}[query["kind"]]
    answer = f"按{label}查询 {start}～{end}（{zone}），共 {len(rows)} 条，全部列出："
    answer += "\n" + (
        "\n".join(f"任务 {i}：{row}" if query["tasks"] else f"{i}. {row}" for i, row in enumerate(rows, 1)) if rows else "没有匹配记录。"
    )
    if unknown:
        answer += f"\n另有 {unknown} 项任务未保存该时间字段，未计入以上结果。"
    return answer
