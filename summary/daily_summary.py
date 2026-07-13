"""Daily summary generation for the P4 scheduled summary stage."""


from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.file_lock import locked_space
from core.llm_client import complete_json
from storage.note_storage import load_index, note_dir


SUMMARY_SYSTEM_PROMPT = """
你是“随心记 Agent”的总结助手。
你要基于用户一段时间内的笔记生成总结。

要求：
- 只能基于 notes，不要编造。
- 先按 type/tags/主题整理，再提炼任务、问题、决定、提醒。
- 如果没有某类内容，不要硬写。
- 输出适合直接发到飞书。
- 必须输出 JSON object：{"summary_markdown":"..."}
"""

REFLECTION_SYSTEM_PROMPT = """
你是“随心记 Agent”的总结审阅器。
请检查草稿是否遗漏重要笔记、是否编造、是否把已完成任务写成待办。
只基于 notes 修订总结。
必须输出 JSON object：{"final_summary":"..."}
"""


RANGE_ALIASES = {
    "今天": "today",
    "today": "today",
    "昨日": "yesterday",
    "昨天": "yesterday",
    "yesterday": "yesterday",
    "一周": "week",
    "最近一周": "week",
    "7天": "week",
    "week": "week",
    "一个月": "month",
    "一月": "month",
    "30天": "month",
    "month": "month",
    "半年": "half_year",
    "六个月": "half_year",
    "half_year": "half_year",
    "一年": "year",
    "12个月": "year",
    "year": "year",
}

RANGE_LABELS = {
    "today": "今天",
    "yesterday": "昨天",
    "week": "最近一周",
    "month": "最近一个月",
    "half_year": "最近半年",
    "year": "最近一年",
}


@dataclass
class SummaryResult:
    range_key: str
    range_label: str
    start: str
    end: str
    note_count: int
    markdown: str
    path: str


def parse_summary_range(raw: str) -> str | None:
    value = raw.strip().lower()
    return RANGE_ALIASES.get(value)


def _local_midnight(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def build_time_range(range_key: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now().astimezone()
    today = _local_midnight(now)
    tomorrow = today + timedelta(days=1)

    if range_key == "today":
        return today, tomorrow
    if range_key == "yesterday":
        return today - timedelta(days=1), today
    if range_key == "week":
        return today - timedelta(days=6), tomorrow
    if range_key == "month":
        return today - timedelta(days=29), tomorrow
    if range_key == "half_year":
        return today - timedelta(days=182), tomorrow
    if range_key == "year":
        return today - timedelta(days=364), tomorrow

    raise ValueError(f"unknown summary range: {range_key}")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return ts


def load_notes_in_range(space_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    notes = []
    for note in load_index(space_id):
        ts = _parse_ts(note.get("ts"))
        if ts is not None and start <= ts < end:
            notes.append(note)

    notes.sort(key=lambda item: item.get("ts", ""))
    return notes


def _clip(text: str | None, limit: int = 260) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "..."


def _brief_notes(notes: list[dict[str, Any]], limit: int = 120) -> list[dict[str, Any]]:
    return [
        {
            "id": note.get("id"),
            "time": note.get("ts"),
            "title": note.get("title"),
            "type": note.get("type"),
            "tags": note.get("tags", []),
            "summary": note.get("summary"),
            "text": _clip(note.get("text")),
        }
        for note in notes[:limit]
    ]


def _stats(notes: list[dict[str, Any]]) -> dict[str, Any]:
    type_counter = Counter(str(note.get("type") or "未分类") for note in notes)
    tag_counter: Counter[str] = Counter()
    for note in notes:
        tag_counter.update(str(tag) for tag in note.get("tags", []))

    return {
        "note_count": len(notes),
        "type_counts": dict(type_counter.most_common()),
        "top_tags": dict(tag_counter.most_common(20)),
    }


def _fallback_summary(range_label: str, notes: list[dict[str, Any]]) -> str:
    if not notes:
        return f"{range_label}没有记录到随心记笔记。"

    stats = _stats(notes)
    lines = [
        f"## {range_label}随心记总结",
        "",
        f"共记录 {stats['note_count']} 条笔记。",
        "",
        "### 分类概览",
    ]
    for note_type, count in stats["type_counts"].items():
        lines.append(f"- {note_type}：{count} 条")

    lines.extend(["", "### 主要记录"])
    for note in notes[:10]:
        date = str(note.get("ts") or "")[:10]
        lines.append(f"- {date}｜{note.get('title') or '无标题'}：{note.get('summary') or note.get('text') or ''}")

    return "\n".join(lines)


def _summary_path(space_id: str, range_key: str, start: datetime, end: datetime) -> Path:
    directory = note_dir(space_id) / "summaries"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{start.date()}_{end.date()}_{range_key}.md"


def save_summary(space_id: str, result: SummaryResult) -> None:
    path = Path(result.path)
    with locked_space(space_id):
        path.write_text(result.markdown + "\n", encoding="utf-8")

        index_path = path.parent / "index.json"
        items = []
        if index_path.exists():
            items = json.loads(index_path.read_text(encoding="utf-8"))

        summary_id = f"{result.start}_{result.end}_{result.range_key}"
        record = {
            "id": summary_id,
            "range_key": result.range_key,
            "range_label": result.range_label,
            "start": result.start,
            "end": result.end,
            "note_count": result.note_count,
            "path": result.path,
            "created_at": datetime.now().astimezone().isoformat(),
        }

        items = [item for item in items if item.get("id") != summary_id]
        items.append(record)
        index_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def generate_summary(space_id: str, range_key: str) -> SummaryResult:
    start, end = build_time_range(range_key)
    range_label = RANGE_LABELS[range_key]
    notes = load_notes_in_range(space_id, start, end)
    path = _summary_path(space_id, range_key, start, end)

    if not notes:
        markdown = _fallback_summary(range_label, notes)
    else:
        payload = {
            "range_label": range_label,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "stats": _stats(notes),
            "notes": _brief_notes(notes),
        }

        try:
            draft = complete_json(
                system_prompt=SUMMARY_SYSTEM_PROMPT,
                user_prompt=json.dumps(payload, ensure_ascii=False, indent=2),
            ).get("summary_markdown", "")

            reviewed = complete_json(
                system_prompt=REFLECTION_SYSTEM_PROMPT,
                user_prompt=json.dumps(
                    {"notes": payload["notes"], "draft": draft},
                    ensure_ascii=False,
                    indent=2,
                ),
            ).get("final_summary", "")

            markdown = str(reviewed or draft).strip() or _fallback_summary(range_label, notes)
        except Exception:
            markdown = _fallback_summary(range_label, notes)

    result = SummaryResult(
        range_key=range_key,
        range_label=range_label,
        start=start.isoformat(),
        end=end.isoformat(),
        note_count=len(notes),
        markdown=markdown,
        path=str(path),
    )
    save_summary(space_id, result)
    return result