"""Validation and safe degradation for AskPlan."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from agent.ask_models import AskPlan, QueryUnit


_INTENT_MEMORY_TYPE = {
    "task_state": "task",
    "task_inventory": "task",
    "preference_current": "preference",
    "semantic_current": "semantic",
    "semantic_history": "semantic",
    "episodic_history": "episodic",
}


def _compact(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def safe_fallback_plan(question: str) -> AskPlan:
    question = " ".join(str(question or "").split()) or "查询相关记录"
    return AskPlan(
        original_query=question,
        units=[QueryUnit(
            id="u1",
            question=question,
            source_spans=[question],
            intent="note_lookup",
            memory_type=None,
            time_mode="all",
            evidence_mode="source_quote",
            need_source_evidence=True,
        )],
        answer_mode="direct",
    )


_TIMELINE_CUES = ("历史", "变化", "经历", "过程", "时间线", "从开始", "从最初", "之前", "后来")
_CURRENT_CUES = ("现在", "当前", "目前", "眼下")
_TASK_INVENTORY_CUES = ("有哪些", "列出", "清单")
_TASK_INVENTORY_DOMAINS = ("任务", "项目", "待办")
_TASK_EVENT_TIME_CUES = ("何时", "什么时候", "截止", "哪天", "几号")
_TASK_EVENT_ACTION_CUES = ("提交", "完成", "参加", "上线", "发布", "交付", "开始")
_AMBIGUOUS_REFERENCE_CUES = ("那个", "这个", "那件", "这件", "它")
_TASK_STATE_CUES = ("怎么样", "进度", "状态", "完成", "做完", "还要", "是否")


def _repair_intent_for_query(unit: QueryUnit, question: str) -> QueryUnit:
    """Apply a small semantic contract repair before dispatch, not answer heuristics."""
    compact = _compact(question)
    unit_compact = _compact(unit.question)
    asks_current_state = unit.time_mode == "current" and any(
        _compact(cue) in unit_compact for cue in _CURRENT_CUES
    )
    if (
        any(_compact(cue) in compact for cue in _AMBIGUOUS_REFERENCE_CUES)
        and any(_compact(cue) in compact for cue in _TASK_STATE_CUES)
    ):
        unit.intent = "task_inventory"
        unit.memory_type = "task"
        unit.time_mode = "current"
        unit.evidence_mode = "inventory"
        return unit
    if any(_compact(cue) in compact for cue in _TIMELINE_CUES) and not asks_current_state:
        if unit.intent == "note_lookup" and any(_compact(cue) in compact for cue in ("经历", "发生", "最近记录")):
            unit.intent = "episodic_history"
            unit.memory_type = "episodic"
            unit.time_mode = "recent"
            unit.evidence_mode = "timeline"
            unit.need_source_evidence = True
            return unit
        if unit.intent not in {"episodic_history", "note_lookup"}:
            unit.intent = "memory_history"
            unit.memory_type = None
            unit.time_mode = "history"
            unit.evidence_mode = "timeline"
            unit.need_source_evidence = True
            return unit
    if (
        any(_compact(cue) in compact for cue in _TASK_EVENT_TIME_CUES)
        and any(_compact(cue) in compact for cue in _TASK_EVENT_ACTION_CUES)
        and unit.intent in {"semantic_current", "note_lookup"}
    ):
        unit.intent = "task_state"
        unit.memory_type = "task"
        unit.time_mode = "current"
        unit.evidence_mode = "current_state"
        return unit
    if unit.evidence_mode == "inventory" or (
        any(_compact(cue) in compact for cue in _TASK_INVENTORY_CUES)
        and any(_compact(domain) in compact for domain in _TASK_INVENTORY_DOMAINS)
    ):
        # An inventory is a list of current task records, never a semantic fact.
        if unit.intent in {"task_state", "semantic_current", "semantic_history", "note_lookup"}:
            unit.intent = "task_inventory"
            unit.memory_type = "task"
            unit.time_mode = "current"
            unit.evidence_mode = "inventory"
    return unit


def _session_texts(session_context: Mapping[str, Any] | None) -> list[str]:
    if not session_context:
        return []
    values: list[str] = []
    for value in session_context.values():
        if isinstance(value, str) and value.strip():
            values.append(value)
        elif isinstance(value, Mapping):
            values.extend(str(item) for item in value.values() if isinstance(item, str) and item.strip())
    return values


def _has_supported_span(unit: QueryUnit, question: str, session_texts: list[str]) -> bool:
    sources = [_compact(question), *(_compact(value) for value in session_texts)]
    return any(_compact(span) and any(_compact(span) in source for source in sources) for span in unit.source_spans)


_ANCHOR_GENERIC_NGRAMS = frozenset({
    "\u4ec0\u4e48", "\u559c\u6b22", "\u4e0d\u559c", "\u6700\u559c", "\u6211\u6700",
    "\u73b0\u5728", "\u5f53\u524d", "\u76ee\u524d", "\u54ea\u91cc", "\u600e\u4e48",
    "\u54ea\u4e2a", "\u8fd9\u4e2a", "\u90a3\u4e2a", "\u6709\u54ea",
})


def _anchor_ngrams(value: object) -> set[str]:
    compact = _compact(value)
    return {
        compact[index:index + 2]
        for index in range(max(0, len(compact) - 1))
        if compact[index:index + 2] not in _ANCHOR_GENERIC_NGRAMS
    }


def _repair_query_anchor(unit: QueryUnit, question: str, session_texts: list[str]) -> QueryUnit:
    """Prevent a planner rewrite from replacing the entity asked by the user."""
    sources = [_compact(question), *(_compact(value) for value in session_texts)]
    anchor = next(
        (span for span in unit.source_spans if _compact(span) and any(_compact(span) in source for source in sources)),
        None,
    )
    if not anchor:
        return unit
    anchor_terms = _anchor_ngrams(anchor)
    question_terms = _anchor_ngrams(unit.question)
    if anchor_terms and not (anchor_terms & question_terms):
        unit.question = anchor
        unit.topic = anchor
    return unit


def _has_cycle(units: list[QueryUnit]) -> bool:
    graph = {unit.id: set(unit.depends_on) for unit in units}
    visited: set[str] = set()
    active: set[str] = set()

    def visit(node: str) -> bool:
        if node in active:
            return True
        if node in visited:
            return False
        visited.add(node)
        active.add(node)
        result = any(visit(parent) for parent in graph.get(node, set()) if parent in graph)
        active.remove(node)
        return result

    return any(visit(node) for node in graph)


def validate_ask_plan(
    raw_plan: AskPlan | Mapping[str, Any] | None,
    *,
    question: str,
    session_context: Mapping[str, Any] | None = None,
    max_units: int = 4,
) -> AskPlan:
    """Return a bounded plan or an evidence-safe Note fallback."""
    try:
        plan = raw_plan if isinstance(raw_plan, AskPlan) else AskPlan.model_validate(raw_plan)
    except (ValidationError, TypeError, ValueError):
        return safe_fallback_plan(question)

    session_texts = _session_texts(session_context)
    accepted: list[QueryUnit] = []
    seen: set[tuple[str, str, str, str]] = set()
    known_ids = {unit.id for unit in plan.units}
    for unit in plan.units[: max(1, min(int(max_units), 6))]:
        unit = _repair_intent_for_query(unit, question)
        expected_type = _INTENT_MEMORY_TYPE.get(unit.intent)
        if expected_type and unit.memory_type not in {None, expected_type}:
            continue
        if expected_type:
            unit.memory_type = expected_type
        if not unit.source_spans or not _has_supported_span(unit, question, session_texts):
            continue
        unit = _repair_query_anchor(unit, question, session_texts)
        unit.depends_on = [item for item in unit.depends_on if item in known_ids and item != unit.id]
        identity = (unit.intent, _compact(unit.topic), unit.time_mode, unit.evidence_mode)
        if identity in seen:
            continue
        seen.add(identity)
        accepted.append(unit)

    accepted_ids = {unit.id for unit in accepted}
    for unit in accepted:
        unit.depends_on = [item for item in unit.depends_on if item in accepted_ids and item != unit.id]
    if not accepted or _has_cycle(accepted):
        return safe_fallback_plan(question)
    return AskPlan(
        original_query=" ".join(str(question or "").split()),
        context=plan.context,
        units=accepted,
        answer_mode=plan.answer_mode,
    )
