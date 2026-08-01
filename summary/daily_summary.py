"""文件作用：时间范围总结生成。

项目关系：本文件依赖 `agent.hooks`、`core.file_lock`、`core.llm_client`、`memory.repository` 等 5 个模块；被 `apps.handlers`、`bot.feishu_bot`、`eval.eval_summary`、`runtime.executor`。
"""




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
    """类功能：`SummaryResult` 封装与“时间范围总结生成”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    range_key: str
    range_label: str
    start: str
    end: str
    note_count: int
    markdown: str
    path: str
    memory_count: int = 0


def parse_summary_range(raw: str) -> str | None:
    """函数功能：`parse_summary_range` 负责解析 summary range，服务于本文件职责：时间范围总结生成。
    传参：
        raw: raw 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
    value = raw.strip().lower()
    return RANGE_ALIASES.get(value)


def _local_midnight(now: datetime) -> datetime:
    """函数功能：`_local_midnight` 负责处理 local midnight，服务于本文件职责：时间范围总结生成。
    传参：
        now: now 参数，由调用方传入，类型为 `datetime`。
    返回结果说明：
        返回 `datetime` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def build_time_range(range_key: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    """函数功能：`build_time_range` 负责构建 time range，服务于本文件职责：时间范围总结生成。
    传参：
        range_key: range key 参数，由调用方传入，类型为 `str`。
        now: now 参数，由调用方传入，类型为 `datetime | None`，默认值为 `None`。
    返回结果说明：
        返回 `tuple[datetime, datetime]`，表示由多个相关值组成的结果。
    """
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
    """函数功能：`_parse_ts` 负责解析 ts，服务于本文件职责：时间范围总结生成。
    传参：
        value: 待转换、校验或计算的值，类型为 `str | None`。
    返回结果说明：
        返回 `datetime | None`；未命中或无需处理时可返回 `None`。
    """
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
    """函数功能：`load_notes_in_range` 负责加载 notes in range，服务于本文件职责：时间范围总结生成。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        start: start 参数，由调用方传入，类型为 `datetime`。
        end: end 参数，由调用方传入，类型为 `datetime`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
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
    """函数功能：`_clip` 负责处理 clip，服务于本文件职责：时间范围总结生成。
    传参：
        text: 输入文本内容，类型为 `str | None`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `260`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "..."


def _brief_notes(notes: list[dict[str, Any]], limit: int = 120) -> list[dict[str, Any]]:
    """函数功能：`_brief_notes` 负责处理 brief notes，服务于本文件职责：时间范围总结生成。
    传参：
        notes: notes 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `120`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
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
    """函数功能：`load_memory_changes` 负责加载 memory changes，服务于本文件职责：时间范围总结生成。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        start: start 参数，由调用方传入，类型为 `datetime`。
        end: end 参数，由调用方传入，类型为 `datetime`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    changes = []
    for memory in list_memories(space_id, status=None, limit=100):
        updated = _parse_ts(memory.updated_at)
        if updated is not None and start <= updated < end:
            changes.append(memory.to_dict())
    changes.sort(key=lambda item: item.get("updated_at", ""))
    return changes


def _brief_memories(memories: list[dict[str, Any]], limit: int = 60) -> list[dict[str, Any]]:
    """函数功能：`_brief_memories` 负责处理 brief memories，服务于本文件职责：时间范围总结生成。
    传参：
        memories: memories 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `60`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
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
    """函数功能：`_stats` 负责处理 stats，服务于本文件职责：时间范围总结生成。
    传参：
        notes: notes 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
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
    """函数功能：`_fallback_summary` 负责处理 fallback summary，服务于本文件职责：时间范围总结生成。
    传参：
        range_label: range label 参数，由调用方传入，类型为 `str`。
        notes: notes 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
        memories: memories 参数，由调用方传入，类型为 `list[dict[str, Any]] | None`，默认值为 `None`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
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
    """函数功能：`_summary_path` 负责处理 summary path，服务于本文件职责：时间范围总结生成。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        range_key: range key 参数，由调用方传入，类型为 `str`。
        start: start 参数，由调用方传入，类型为 `datetime`。
        end: end 参数，由调用方传入，类型为 `datetime`。
    返回结果说明：
        返回 `Path` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    directory = note_dir(space_id) / "summaries"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{start.date()}_{end.date()}_{range_key}.md"


def save_summary(space_id: str, result: SummaryResult) -> None:
    """函数功能：`save_summary` 负责保存 summary，服务于本文件职责：时间范围总结生成。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        result: 上游步骤返回的结果对象，类型为 `SummaryResult`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`_summary_complete_json` 负责完成 json，服务于本文件职责：时间范围总结生成。
    传参：
        context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext | None`。
        name: name 参数，由调用方传入，类型为 `str`。
        system_prompt: system prompt 参数，由调用方传入，类型为 `str`。
        user_prompt: user prompt 参数，由调用方传入，类型为 `str`。
        range_key: range key 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    llm_task = "summary_review" if name == "summary_review" else "summary_draft"
    def call() -> dict[str, Any]:
        """函数功能：`call` 负责调用，服务于本文件职责：时间范围总结生成。
        传参：
            无。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
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
    """函数功能：`_generate_summary_impl` 负责生成 summary impl，服务于本文件职责：时间范围总结生成。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        range_key: range key 参数，由调用方传入，类型为 `str`。
        context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext | None`。
    返回结果说明：
        返回 `SummaryResult` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
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
    """函数功能：`generate_summary` 负责生成 summary，服务于本文件职责：时间范围总结生成。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        range_key: range key 参数，由调用方传入，类型为 `str`。
        tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`，默认值为 `'default'`。
        user_id: 用户标识，用于鉴权、限流、会话和数据归属，类型为 `str | None`，默认值为 `None`。
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str | None`，默认值为 `None`。
        task_id: 任务标识，用于查询、更新或幂等处理任务状态，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `SummaryResult` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
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
