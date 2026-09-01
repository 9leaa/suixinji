"""LLM-backed planner for the bounded Ask workflow."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from core import settings
from core.llm_client import complete_json

from agent.ask_models import AskPlan, QueryUnit
from agent.ask_plan_validator import safe_fallback_plan, validate_ask_plan


ASK_PLANNER_PROMPT = """你是随心记的 Ask 查询规划器。只输出一个 JSON object，不输出 Markdown。

你的任务不是回答问题，也不是选择工具；只把用户问题转为背景 context 和 1 到 N 个可独立检索的 Query Unit。

固定 Schema：
{
  "original_query":"...",
  "context":[{"text":"...","source":"current_message|session_context"}],
  "units":[{
    "id":"u1",
    "question":"...",
    "source_spans":["用户原话片段"],
    "intent":"task_state|task_inventory|preference_current|semantic_current|semantic_history|episodic_history|note_lookup|memory_history",
    "memory_type":"task|preference|semantic|episodic|null",
    "facet":"identity|location|education|career|project|learning|capability|device|other|null",
    "topic":"...|null",
    "time_mode":"current|recent|history|all",
    "evidence_mode":"current_state|inventory|timeline|source_quote|aggregate",
    "need_source_evidence":true,
    "depends_on":["u1"],
    "priority":1
  }],
  "answer_mode":"direct|list|timeline|compare|causal|summary"
}

规则：
- 每次必须输出 1 到 {max_units} 个 Unit；不要为了数量强行拆分。
- 单句可能有多个问题，多句也可能只有一个问题；只有可独立检索、独立回答的目标才拆分。
- 纯背景或时间线索放 context，不要变成 Unit。
- 每个 Unit 的 source_spans 必须逐字来自用户问题；使用会话承接时 source 可以是 session_context，但不得编造事实。
- 关系、比较、因果问题可以依赖前序事实 Unit；depends_on 不能形成环。
- task_state 对应一个明确 task 的当前状态；task_inventory 对应一组当前任务、项目、重点或待办；preference_current 对应 preference；semantic_current/history 对应 semantic；episodic_history 对应 episodic。
- 出现“历史、变化、过程、经历、从开始到完成”等时间演进需求时，使用 memory_history，topic 只保留被询问的对象短语。
- 问“有哪些项目/任务、主要在忙什么、当前重点、列出待办”等集合问题时，使用 task_inventory，evidence_mode=inventory；不要把它伪装成一个 semantic fact。
- 不得输出工具名、Memory ID、Note ID、分数、检索阈值、任务状态结论、偏好极性结论或事实结论。
"""


# The planner normally decomposes a question. These are broad retrieval
# domains for its unavailable-path, never dataset-specific question patterns.
_MULTI_GOAL_DOMAINS = (
    (("偏好", "饮食", "口味", "饮料", "咖啡", "食物", "吃", "喝"), "preference_current", "preference", None, "饮食偏好"),
    (("设备", "电脑", "手机", "键盘", "显示器", "耳机"), "semantic_current", "semantic", "device", "设备"),
    (("语言", "技术栈", "编程", "擅长", "能力"), "semantic_current", "semantic", "capability", "语言能力"),
)


def _fallback_multi_goal_units(question: str) -> list[QueryUnit]:
    compact = "".join(question.split()).casefold()
    matched = [domain for domain in _MULTI_GOAL_DOMAINS if any(marker in compact for marker in domain[0])]
    if len(matched) < 2:
        return []
    return [
        QueryUnit(
            id=f"u{index}", question=label, source_spans=[question],
            intent=intent, memory_type=memory_type, facet=facet, topic=label,
            time_mode="current", evidence_mode="current_state",
        )
        for index, (_markers, intent, memory_type, facet, label) in enumerate(matched, start=1)
    ]


def deterministic_fallback_plan(question: str) -> AskPlan:
    """Translate the legacy deterministic router into the same bounded AskPlan contract."""
    from agent.query_agent import _deterministic_route

    multi_units = _fallback_multi_goal_units(question)
    if multi_units:
        return validate_ask_plan(AskPlan(
            original_query=question, units=multi_units, answer_mode="summary",
        ), question=question)

    route = _deterministic_route(question) or {}
    args = route.get("args") if isinstance(route.get("args"), Mapping) else {}
    action = str(route.get("action") or "")
    memory_type = str(args.get("memory_type") or "")
    intent_map = {
        "memory_history": ("memory_history", None, "history", "timeline"),
        "list_tasks": ("task_inventory", "task", "current", "inventory"),
        "task_status_search": ("task_state", "task", "current", "current_state"),
        "list_recent_episodes": ("episodic_history", "episodic", "recent", "timeline"),
        # Legacy semantic_search searches Notes; it is not semantic Memory.
        "semantic_search": ("note_lookup", None, "all", "source_quote"),
    }
    if action == "memory_search":
        intent_map["memory_search"] = {
            "task": ("task_state", "task", "current", "current_state"),
            "preference": ("preference_current", "preference", "current", "current_state"),
            "semantic": ("semantic_current", "semantic", "current", "current_state"),
        }.get(memory_type, ("note_lookup", None, "all", "source_quote"))
    intent, resolved_type, time_mode, evidence_mode = intent_map.get(
        action, ("note_lookup", None, "all", "source_quote")
    )
    plan = AskPlan(
        original_query=question,
        units=[QueryUnit(
            id="u1",
            question=question,
            source_spans=[question],
            intent=intent,
            memory_type=resolved_type,
            time_mode=time_mode,
            evidence_mode=evidence_mode,
            need_source_evidence=intent in {"memory_history", "note_lookup"},
        )],
        answer_mode="timeline" if intent == "memory_history" else ("list" if intent == "task_inventory" else "direct"),
    )
    return validate_ask_plan(plan, question=question)


def plan_ask(
    question: str,
    *,
    session_context: Mapping[str, Any] | None = None,
    max_units: int = 4,
) -> AskPlan:
    question = " ".join(str(question or "").split())
    if not question:
        return safe_fallback_plan(question)
    payload = {
        "question": question,
        "session_context": dict(session_context or {}),
        "max_units": max(1, min(int(max_units), 6)),
    }
    # Planner availability must not turn a known read-only query into a blind Note lookup.
    # Two bounded attempts absorb transient provider errors; the deterministic
    # router then provides a contract-compatible fallback without exposing tools
    # to the answer model.
    for _attempt in range(2):
        try:
            raw = complete_json(
                system_prompt=ASK_PLANNER_PROMPT.replace("{max_units}", str(payload["max_units"])),
                user_prompt=json.dumps(payload, ensure_ascii=False),
                model_role="fast",
                llm_task="query_intent",
                timeout_seconds=getattr(settings, "ASK_PLANNER_TIMEOUT_SECONDS", 12),
            )
            return validate_ask_plan(raw, question=question, session_context=session_context, max_units=payload["max_units"])
        except Exception:
            continue
    return deterministic_fallback_plan(question)
