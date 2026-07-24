"""Daily summary generation for the P4 scheduled summary stage."""


from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent.hooks import AgentRunContext, get_default_hook_manager
from core.file_lock import locked_space
from core.llm_client import complete_json
from memory.repository import list_memories
from storage.note_storage import is_note_queryable, load_index, note_dir


SUMMARY_SYSTEM_PROMPT = """
你是“随心记 Agent”的总结助手。
你要基于用户一段时间内的笔记证据和长期记忆状态生成两层总结。

要求：
- 只能基于 notes 和 memory_changes，不要编造。
- 先按 type/tags/主题整理，再提炼任务、问题、决定、提醒。
- 明确区分“这段时间发生了什么”和“目标、偏好、任务状态发生了什么变化”。
- 如果没有某类内容，不要硬写。
- 输出适合直接发到飞书。
- 必须输出 JSON object：{"summary_markdown":"..."}
"""

REFLECTION_SYSTEM_PROMPT = """
你是“随心记 Agent”的总结审阅器。
请检查草稿是否遗漏重要笔记或记忆变化、是否编造、是否把已完成任务写成待办。
只基于 notes 和 memory_changes 修订总结。
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
    memory_count: int = 0


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
        if not is_note_queryable(note):
            continue
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


def load_memory_changes(space_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    changes = []
    for memory in list_memories(space_id, status=None, limit=100):
        updated = _parse_ts(memory.updated_at)
        if updated is not None and start <= updated < end:
            changes.append(memory.to_dict())
    changes.sort(key=lambda item: item.get("updated_at", ""))
    return changes


def _brief_memories(memories: list[dict[str, Any]], limit: int = 60) -> list[dict[str, Any]]:
    return [
        {
            "id": memory.get("id"),
            "memory_type": memory.get("memory_type"),
            "content": _clip(memory.get("content"), 360),
            "status": memory.get("status"),
            "task_status": memory.get("task_status"),
            "updated_at": memory.get("updated_at"),
            "source_note_ids": [source.get("note_id") for source in (memory.get("sources") or [])[:8]],
        }
        for memory in memories[:limit]
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


def _fallback_summary(range_label: str, notes: list[dict[str, Any]], memories: list[dict[str, Any]] | None = None) -> str:
    if not notes and not memories:
        return f"{range_label}没有记录到随心记笔记。"

    stats = _stats(notes)
    lines = [
        f"## {range_label}随心记总结",
        "",
        f"共记录 {stats['note_count']} 条笔记。",
    ]
    if notes:
        lines.extend(["", "### 分类概览"])
        for note_type, count in stats["type_counts"].items():
            lines.append(f"- {note_type}：{count} 条")

        lines.extend(["", "### 主要记录"])
        for note in notes[:10]:
            date = str(note.get("ts") or "")[:10]
            lines.append(f"- {date}｜{note.get('title') or '无标题'}：{note.get('summary') or note.get('text') or ''}")

    if memories:
        lines.extend(["", "### 长期状态变化"])
        for memory in memories[:10]:
            task_status = f"｜{memory.get('task_status')}" if memory.get("task_status") else ""
            lines.append(f"- {memory.get('memory_type')}｜{memory.get('status')}{task_status}：{memory.get('content')}")

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
            "memory_count": result.memory_count,
            "path": result.path,
            "created_at": datetime.now().astimezone().isoformat(),
        }

        items = [item for item in items if item.get("id") != summary_id]
        items.append(record)
        index_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _summary_complete_json(
    context: AgentRunContext | None,
    *,
    name: str,
    system_prompt: str,
    user_prompt: str,
    range_key: str,
) -> dict[str, Any]:
    llm_task = "summary_review" if name == "summary_review" else "summary_draft"
    def call() -> dict[str, Any]:
        try:
            return complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                llm_task=llm_task,
                route_context={"range_key": range_key},
            )
        except TypeError as exc:
            if "llm_task" not in str(exc) and "route_context" not in str(exc):
                raise
            return complete_json(system_prompt=system_prompt, user_prompt=user_prompt)

    if context is None:
        return call()
    return get_default_hook_manager().run_llm(
        context,
        {"name": name, "system_prompt_len": len(system_prompt), "user_prompt": user_prompt, "llm_task": llm_task},
        call,
    )


def _generate_summary_impl(space_id: str, range_key: str, context: AgentRunContext | None) -> SummaryResult:
    start, end = build_time_range(range_key)
    range_label = RANGE_LABELS[range_key]
    notes = load_notes_in_range(space_id, start, end)
    memories = load_memory_changes(space_id, start, end)
    path = _summary_path(space_id, range_key, start, end)

    if not notes:
        markdown = _fallback_summary(range_label, notes, memories)
    else:
        payload = {
            "range_label": range_label,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "stats": _stats(notes),
            "notes": _brief_notes(notes),
            "memory_changes": _brief_memories(memories),
        }

        try:
            draft = _summary_complete_json(
                context,
                name="summary_draft",
                system_prompt=SUMMARY_SYSTEM_PROMPT,
                user_prompt=json.dumps(payload, ensure_ascii=False, indent=2),
                range_key=range_key,
            ).get("summary_markdown", "")

            reviewed = _summary_complete_json(
                context,
                name="summary_review",
                system_prompt=REFLECTION_SYSTEM_PROMPT,
                user_prompt=json.dumps(
                    {"notes": payload["notes"], "memory_changes": payload["memory_changes"], "draft": draft},
                    ensure_ascii=False,
                    indent=2,
                ),
                range_key=range_key,
            ).get("final_summary", "")

            markdown = str(reviewed or draft).strip() or _fallback_summary(range_label, notes, memories)
        except Exception:
            markdown = _fallback_summary(range_label, notes, memories)

    result = SummaryResult(
        range_key=range_key,
        range_label=range_label,
        start=start.isoformat(),
        end=end.isoformat(),
        note_count=len(notes),
        markdown=markdown,
        path=str(path),
        memory_count=len(memories),
    )
    save_summary(space_id, result)
    return result


def generate_summary(
    space_id: str,
    range_key: str,
    *,
    tenant_id: str = "default",
    user_id: str | None = None,
    message_id: str | None = None,
    task_id: str | None = None,
) -> SummaryResult:
    context = AgentRunContext.create(
        space_id=space_id,
        run_type="summary",
        tenant_id=tenant_id,
        user_id=user_id,
        message_id=message_id,
        task_id=task_id,
        metadata={"range_key": range_key},
    )
    manager = get_default_hook_manager()
    return manager.run_agent(
        context,
        lambda: manager.run_tool(
            context,
            "generate_summary",
            {"range_key": range_key},
            lambda: _generate_summary_impl(space_id, range_key, context),
        ),
    )
