"""文件作用：问答主编排。

项目关系：本文件依赖 `agent.hooks`、`agent.query_intent`、`agent.query_planner`、`agent.query_route_features` 等 16 个模块；被 `apps.handlers`、`bot.feishu_bot`、`runtime.executor`、`scripts.smoke_distributed_hooks`。
"""



from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from agent.hooks import AgentRunContext, get_default_hook_manager
from agent.query_intent import classify_query_intent, is_task_inventory_question, route_for_intent
from agent.query_planner import build_query_plan
from agent.query_route_features import should_call_query_intent_llm, structural_route
from core import settings
from core.llm_client import complete_json, embed_text
from core.observability import log_event, observe
from core.sensitive import mentions_sensitive_topic
from core.settings import MEMORY_QUERY_MIN_SCORE, QUERY_MIN_SCORE, QUERY_TOP_K, STORAGE_BACKEND
from core.taxonomy import is_valid_tag, is_valid_type, normalize_tag, normalize_type
from memory.service import memory_search
from memory.access import AccessContext
from memory.repository import get_memory, get_memory_timeline, list_memory_decisions, list_memories
from memory.consistency import wait_for_memory_barrier
from memory.trace import add_step, finish_trace, start_trace
from storage.note_storage import is_note_queryable, load_index
from storage.vector_store import search_related

if STORAGE_BACKEND == "postgres":
    from repositories.postgres.notes import (
        find_note as _postgres_find_note,
        get_note_relations as _postgres_get_note_relations,
        list_provisional_notes as _postgres_list_provisional_notes,
        list_recent_notes as _postgres_list_recent_notes,
        query_notes_by_tags as _postgres_query_notes_by_tags,
        query_notes_by_type as _postgres_query_notes_by_type,
        hybrid_search_notes as _postgres_hybrid_search_notes,
        search_notes_memory_fallback as _postgres_search_notes_memory_fallback,
    )


DEFAULT_QUERY_MIN_SCORE = QUERY_MIN_SCORE
DEFAULT_MEMORY_MIN_SCORE = MEMORY_QUERY_MIN_SCORE


REACT_SYSTEM_PROMPT = f"""
你是“随心记 Agent”的查询助手。

你可以使用工具查询用户的历史笔记，然后回答问题。

可用工具：
1. filter_notes(type, tags, match_all_tags, limit): 当用户明确给出固定 type 或固定 tags 条件时，直接筛选 index.json。
2. semantic_search(query, top_k, min_score): 当用户没有明确 type/tags，或忘记分类条件时，按语义搜索相关笔记。
3. list_recent(days, limit): 查看最近若干天笔记。
4. get_note(note_id): 按 id 读取一条完整笔记。
5. follow_links(note_id, limit): 查看某条笔记 related 关联的笔记，包括它指向的笔记和指向它的笔记。
6. memory_search(query, memory_type, limit, min_score): 查询长期记忆，适合用户事实、偏好、任务状态和长期背景。

每一步只能输出 JSON object。

如果需要调用工具，输出：
{{"thought":"为什么要调用这个工具","action":"semantic_search","args":{{"query":"用户问题","top_k":{QUERY_TOP_K},"min_score":{QUERY_MIN_SCORE}}}}}

如果已经有足够证据回答，输出：
{{"thought":"为什么可以回答","final_answer":"基于笔记的回答","evidence_ids":["memory 或 note 的 id"]}}

规则：
- 如果用户明确说“type 是生活/学习/任务”等，调用 filter_notes，不要调用 semantic_search。
- 如果用户明确说“标签是饮食/提醒/问题”等，调用 filter_notes，不要调用 semantic_search。
- 如果用户同时给出 type 和 tags，调用 filter_notes。
- 用户没有明确 type/tags，或只是用自然语言描述想找的内容时，才调用 semantic_search。
- 用户通常不知道 note_id。调用 follow_links 前，必须先通过 semantic_search、filter_notes 或 list_recent 找到候选 note_id。
- 如果用户问长期偏好、习惯、当前任务状态或“我现在/我喜欢/我住在哪/我重点做什么”，优先调用 memory_search。
- 如果用户问“和某条笔记相关的有哪些”，先 semantic_search 找候选 note_id，再 follow_links。
- 回答只能基于 observations，不要编造。
- 输出 final_answer 时，必须给出直接支撑该回答的 evidence_ids；只能填写 observations 中实际出现过的 id。若没有直接证据，返回空数组并明确说明无法确认。
- 如果没有找到相关笔记，要明确说没找到。
- observations 中如果存在 session_context，它表示上一轮临时会话状态；可据此理解“一周”“这个”等承接回答。
- 如果回答后需要等待用户补充信息，可额外输出 session_update，例如 {{"waiting_for":"summary_range","current_intent":"summary"}}；不再需要时输出空对象。
- 回答要自然、简洁，必要时引用笔记标题或时间。
"""

FINAL_SYSTEM_PROMPT = """
你是“随心记 Agent”的最终回答器。

请只基于给定 observations 回答用户问题。
必须输出 JSON object：
{"final_answer":"..."}
"""

_COMPLEX_QUERY_MARKERS = ("比较", "为什么", "结合", "关联", "之间", "变化", "趋势", "总结", "归纳", "多次")
_CURRENT_PREFERENCE_MARKERS = ("喜欢", "讨厌", "偏好", "习惯", "过敏", "避开")
_CURRENT_TASK_MARKERS = ("当前待办", "现在的任务", "有哪些任务", "要做什么", "任务进度", "待办是什么", "当前状态", "什么状态", "进展如何", "是否完成", "有没有完成", "做到哪")
_CURRENT_FACT_MARKERS = ("住在哪里", "住哪", "现在住", "目前住", "正在学习", "重点做什么", "重点是什么", "当前重点", "当前项目", "focus")
_HISTORY_MARKERS = ("历史", "之前", "以前", "最初", "变化", "变成", "经历", "完成前", "过去", "版本")
_LIST_MARKERS = ("列出", "列表", "所有任务", "任务清单", "最近几件", "最近的经历", "概括画像", "用户画像", "个人画像")


def _clip(text: str | None, limit: int = 500) -> str:
    """函数功能：`_clip` 负责处理 clip，服务于本文件职责：问答主编排。
    传参：
        text: 输入文本内容，类型为 `str | None`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `500`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    if not text:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _parse_ts(ts: str | None) -> datetime | None:
    """函数功能：`_parse_ts` 负责解析 ts，服务于本文件职责：问答主编排。
    传参：
        ts: ts 参数，由调用方传入，类型为 `str | None`。
    返回结果说明：
        返回 `datetime | None`；未命中或无需处理时可返回 `None`。
    """
    if not ts:
        return None
    try:
        value = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return value


def _coerce_tags(value: Any) -> list[str]:
    """函数功能：`_coerce_tags` 负责处理 coerce tags，服务于本文件职责：问答主编排。
    传参：
        value: 待转换、校验或计算的值，类型为 `Any`。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _coerce_bool(value: Any, default: bool = True) -> bool:
    """函数功能：`_coerce_bool` 负责处理 coerce bool，服务于本文件职责：问答主编排。
    传参：
        value: 待转换、校验或计算的值，类型为 `Any`。
        default: default 参数，由调用方传入，类型为 `bool`，默认值为 `True`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"0", "false", "no", "n", "否", "不是"}:
            return False
        if lowered in {"1", "true", "yes", "y", "是"}:
            return True
    return bool(value)


def _normalized_query(value: str) -> str:
    """函数功能：`_normalized_query` 负责查询 normalized，服务于本文件职责：问答主编排。
    传参：
        value: 待转换、校验或计算的值，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return " ".join(value.strip().casefold().rstrip("?？").split())


def _deterministic_route(question: str) -> dict[str, Any] | None:
    """函数功能：`_deterministic_route` 负责路由 deterministic，服务于本文件职责：问答主编排。
    传参：
        question: 用户问题文本，类型为 `str`。
    返回结果说明：
        返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
    """
    normalized = _normalized_query(question)
    type_match = re.fullmatch(r"/(?:type|类型)\s+(.+)", normalized)
    tag_match = re.fullmatch(r"/(?:tag|标签)\s+(.+)", normalized)
    if type_match:
        return {
            "action": "filter_notes",
            "args": {"type": type_match.group(1).strip(), "limit": 30},
            "synthesize": False,
            "reason": "explicit_type_filter",
        }
    if tag_match:
        return {
            "action": "filter_notes",
            "args": {"tags": [tag_match.group(1).strip()], "limit": 30},
            "synthesize": False,
            "reason": "explicit_tag_filter",
        }

    natural_type = re.search(r"(?:type|类型)\s*(?:是|=|:|：)?\s*([^\s，。？?]+)", normalized)
    natural_tag = re.search(r"(?:tag|标签)\s*(?:是|=|:|：)?\s*([^\s，。？?]+)", normalized)
    if natural_type and is_valid_type(natural_type.group(1)):
        return {
            "action": "filter_notes",
            "args": {"type": natural_type.group(1), "limit": 30},
            "synthesize": False,
            "reason": "structured_type_filter",
        }
    if natural_tag and is_valid_tag(natural_tag.group(1)):
        return {
            "action": "filter_notes",
            "args": {"tags": [natural_tag.group(1)], "limit": 30},
            "synthesize": False,
            "reason": "structured_tag_filter",
        }
    history_synthesis = any(marker in normalized for marker in ("状态变化", "经历了哪些", "从开始到完成", "开始到完成", "过程", "总结", "归纳"))
    current_fact_ask = any(marker in normalized for marker in ("现在", "当前", "目前", "focus", "重点是什么", "当前重点"))
    if history_synthesis:
        return {
            "action": "memory_history",
            "args": {"query": normalized, "limit": 10},
            "synthesize": True,
            "reason": "history_synthesis_timeline",
        }
    if any(marker in normalized for marker in _LIST_MARKERS):
        constraints = parse_list_constraints(normalized, default_limit=3)
        list_limit = int(constraints["limit"])
        if constraints["memory_type"] == "episodic":
            return {
                "action": "list_recent_episodes",
                "args": {"limit": list_limit, "query": normalized},
                "synthesize": True,
                "reason": "episode_inventory",
            }
        if constraints["memory_type"] == "task":
            return {
                "action": "list_tasks",
                "args": {"limit": list_limit, "status": constraints["status"], "query": normalized},
                "fallback": {"action": "filter_notes", "args": {"type": "任务", "limit": 30}},
                "synthesize": True,
                "reason": "task_inventory",
            }
        return {
            "action": "profile_summary",
            "args": {"query": normalized, "limit": list_limit},
            "synthesize": True,
            "reason": "profile_summary",
        }
    if any(marker in normalized for marker in _HISTORY_MARKERS) and not current_fact_ask and not any(marker in normalized for marker in ("比较", "结合", "关联", "趋势", "总结", "归纳", "多次")):
        return {
            "action": "memory_history",
            "args": {"query": normalized, "limit": 10},
            "synthesize": True,
            "reason": "explicit_history_timeline",
        }
    if "最近" in normalized and any(marker in normalized for marker in ("笔记", "记录", "记了", "写了")):
        return {
            "action": "list_recent",
            "args": {"days": 7, "limit": 10},
            "synthesize": False,
            "reason": "recent_notes",
        }
    if any(marker in normalized for marker in _CURRENT_PREFERENCE_MARKERS):
        return {
            "action": "memory_search",
            "args": {"query": normalized, "memory_type": "preference", "limit": 5, "min_score": DEFAULT_MEMORY_MIN_SCORE},
            "fallback": {
                "action": "memory_note_fallback",
                "args": {"query": normalized, "limit": QUERY_TOP_K, "min_score": DEFAULT_QUERY_MIN_SCORE},
            },
            "synthesize": True,
            "reason": "current_preference",
        }
    if any(marker in normalized for marker in _CURRENT_TASK_MARKERS):
        fallback: dict[str, Any] = {
            "action": "memory_note_fallback",
            "args": {"query": normalized, "limit": QUERY_TOP_K, "min_score": DEFAULT_QUERY_MIN_SCORE},
        }
        if is_task_inventory_question(normalized):
            fallback = {
                "action": "filter_notes",
                "args": {"type": "任务", "limit": 30},
            }
        return {
            "action": "memory_search",
            "args": {"query": normalized, "memory_type": "task", "limit": 8, "min_score": DEFAULT_MEMORY_MIN_SCORE},
            "fallback": fallback,
            "synthesize": True,
            "reason": "current_task",
        }
    if any(marker in normalized for marker in _CURRENT_FACT_MARKERS):
        return {
            "action": "memory_search",
            "args": {"query": normalized, "memory_type": "semantic", "limit": 5, "min_score": DEFAULT_MEMORY_MIN_SCORE},
            "fallback": {
                "action": "memory_note_fallback",
                "args": {"query": normalized, "limit": QUERY_TOP_K, "min_score": DEFAULT_QUERY_MIN_SCORE},
            },
            "synthesize": True,
            "reason": "current_fact",
        }
    _, structural_decision = structural_route(normalized)
    if len(normalized) <= 60 and structural_decision.complexity == "simple":
        return {
            "action": "semantic_search",
            "args": {"query": normalized, "top_k": QUERY_TOP_K, "min_score": DEFAULT_QUERY_MIN_SCORE},
            "synthesize": True,
            "reason": "single_hop_semantic",
        }
    return None


def _intent_route(question: str, *, trace: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """函数功能：`_intent_route` 负责路由 intent，服务于本文件职责：问答主编排。
    传参：
        question: 用户问题文本，类型为 `str`。
        trace: trace 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
    """
    if not settings.QUERY_INTENT_MODEL_ENABLED:
        return None
    if settings.QUERY_ROUTER_V2_ENABLED and settings.QUERY_ROUTER_LLM_ON_UNCERTAIN and not should_call_query_intent_llm(question):
        return None
    intent = classify_query_intent(question)
    if intent is None:
        add_step(trace, "query_intent_classified", status="partial", reason="invalid_or_unavailable_model_output")
        return None
    add_step(
        trace,
        "query_intent_classified",
        output_summary={
            "intent": intent.intent,
            "time_scope": intent.time_scope,
            "confidence": intent.confidence,
            "complexity": intent.complexity,
            "strategies": list(intent.strategies),
        },
        reason="fast_structured_classifier",
    )
    return route_for_intent(
        intent,
        question,
        memory_min_score=DEFAULT_MEMORY_MIN_SCORE,
        note_min_score=DEFAULT_QUERY_MIN_SCORE,
        top_k=QUERY_TOP_K,
    )


def _safe_tool_args(action: str, args: dict[str, Any]) -> dict[str, Any]:
    """函数功能：`_safe_tool_args` 负责处理 safe tool args，服务于本文件职责：问答主编排。
    传参：
        action: action 参数，由调用方传入，类型为 `str`。
        args: args 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    safe: dict[str, Any] = {"tool": action}
    for key in ("type", "note_type", "tags", "tag", "limit", "top_k", "min_score", "days", "note_id", "match_all_tags", "memory_type", "status", "operation"):
        if key in args:
            safe[key] = args.get(key)
    if "query" in args:
        safe["query_len"] = len(str(args.get("query") or ""))
    return safe


def _result_ids(result: Any) -> list[str]:
    """函数功能：`_result_ids` 负责处理 result ids，服务于本文件职责：问答主编排。
    传参：
        result: 上游步骤返回的结果对象，类型为 `Any`。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
    ids: list[str] = []
    for item in _evidence_items(result):
        item_id = item.get("id") or item.get("memory_id")
        if item_id:
            ids.append(str(item_id))
    return list(dict.fromkeys(ids))[:10]


def _low_quality_memory_result(result: Any) -> bool:
    """函数功能：`_low_quality_memory_result` 负责处理 low quality memory result，服务于本文件职责：问答主编排。
    传参：
        result: 上游步骤返回的结果对象，类型为 `Any`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    if not isinstance(result, list) or not result:
        return True
    scores = [float(item.get("score") or 0.0) for item in result if isinstance(item, dict)]
    if not scores:
        return True
    top = scores[0]
    if top < 0.58:
        return True
    return len(scores) > 1 and top < 0.75 and top - scores[1] < 0.04


def _fuse_memory_results(groups: list[list[dict[str, Any]]], *, limit: int = 5) -> list[dict[str, Any]]:
    """函数功能：`_fuse_memory_results` 负责处理 fuse memory results，服务于本文件职责：问答主编排。
    传参：
        groups: groups 参数，由调用方传入，类型为 `list[list[dict[str, Any]]]`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `5`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    candidates: dict[str, dict[str, Any]] = {}
    fusion: dict[str, float] = {}
    appearances: dict[str, int] = {}
    for group_index, group in enumerate(groups):
        weight = 1.0 if group_index == 0 else 0.92
        for rank, item in enumerate(group, start=1):
            if not isinstance(item, dict) or not item.get("id"):
                continue
            item_id = str(item["id"])
            candidates.setdefault(item_id, dict(item))
            appearances[item_id] = appearances.get(item_id, 0) + 1
            score = float(item.get("score") or 0.0)
            contribution = weight * (0.70 * score + 0.30 / rank)
            fusion[item_id] = max(fusion.get(item_id, 0.0), contribution)
    for item_id, count in appearances.items():
        fusion[item_id] += min(0.12, 0.03 * (count - 1))
    ordered = sorted(candidates, key=lambda item_id: (fusion[item_id], candidates[item_id].get("updated_at") or "", item_id), reverse=True)
    result: list[dict[str, Any]] = []
    for item_id in ordered[: max(1, int(limit))]:
        item = candidates[item_id]
        item["retrieval_fusion_score"] = round(fusion[item_id], 4)
        result.append(item)
    return result


def _mixed_language_query_rewrites(question: str) -> list[str]:
    """Return generic intent-preserving rewrites alongside the original query."""
    if not settings.QUERY_MIXED_LANGUAGE_REWRITE_ENABLED:
        return []
    normalized = _normalized_query(question)
    latin_words = set(re.findall(r"[a-z]+", normalized))
    rewrites: list[str] = []
    current = bool(latin_words & {"current", "now", "currently", "recently"})
    focus = bool(latin_words & {"focus", "mainly", "working", "work", "project"})
    if current and focus:
        rewrites.append("我现在主要在做什么？")
    elif "focus" in latin_words:
        rewrites.append("我当前的工作重点是什么？")
    if "focus" in normalized and any(marker in normalized for marker in ("现在", "当前", "目前")):
        rewrites.append("我当前的工作重点是什么？")
    return list(dict.fromkeys(rewrite for rewrite in rewrites if _normalized_query(rewrite) != normalized))


def _evidence_items(evidence: Any) -> list[dict[str, Any]]:
    """函数功能：`_evidence_items` 负责处理 evidence items，服务于本文件职责：问答主编排。
    传参：
        evidence: evidence 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    items: list[dict[str, Any]] = []
    if isinstance(evidence, list):
        items.extend(item for item in evidence if isinstance(item, dict))
    elif isinstance(evidence, dict):
        if evidence.get("id") or evidence.get("memory_id"):
            items.append(evidence)
        items.extend(item for item in evidence.get("related", []) if isinstance(item, dict))
        items.extend(item for item in evidence.get("candidates", []) if isinstance(item, dict))
        slots = evidence.get("slots")
        if isinstance(slots, dict):
            for values in slots.values():
                if isinstance(values, list):
                    items.extend(item for item in values if isinstance(item, dict))
    return items


def _merge_evidence(current: list[dict[str, Any]], result: Any) -> list[dict[str, Any]]:
    """函数功能：`_merge_evidence` 负责合并 evidence，服务于本文件职责：问答主编排。
    传参：
        current: current 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
        result: 上游步骤返回的结果对象，类型为 `Any`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    merged = list(current)
    seen = {str(item.get("id")) for item in merged if item.get("id")}
    for item in _evidence_items(result):
        item_id = str(item.get("id") or "")
        if item_id and item_id not in seen:
            merged.append(item)
            seen.add(item_id)
    return merged


def _cited_evidence(observations: list[dict[str, Any]], evidence_ids: Any) -> list[dict[str, Any]]:
    """函数功能：`_cited_evidence` 负责处理 cited evidence，服务于本文件职责：问答主编排。
    传参：
        observations: observations 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
        evidence_ids: evidence ids 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    if not isinstance(evidence_ids, list):
        return []
    available: dict[str, dict[str, Any]] = {}
    for observation in observations:
        for item in _evidence_items(observation.get("result")):
            item_id = str(item.get("id") or "")
            if item_id and item_id not in available:
                available[item_id] = item
    cited: list[dict[str, Any]] = []
    seen: set[str] = set()
    for evidence_id in evidence_ids:
        item_id = str(evidence_id or "")
        if item_id and item_id in available and item_id not in seen:
            cited.append(available[item_id])
            seen.add(item_id)
    return cited


def _source_lines(
    selected_evidence: Any,
    *,
    memory_limit: int = 5,
    note_limit: int = 5,
) -> list[str]:
    """函数功能：`_source_lines` 负责处理 source lines，服务于本文件职责：问答主编排。
    传参：
        selected_evidence: selected evidence 参数，由调用方传入，类型为 `Any`。
        memory_limit: memory limit 参数，由调用方传入，类型为 `int`，默认值为 `5`。
        note_limit: note limit 参数，由调用方传入，类型为 `int`，默认值为 `5`。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
    lines: list[str] = []
    seen: set[str] = set()
    memory_count = 0
    note_count = 0

    for item in _evidence_items(selected_evidence):
        item_id = str(item.get("memory_id") or item.get("id") or "")
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        if item.get("memory_type"):
            if memory_count >= memory_limit:
                continue
            source_count = len(item.get("sources") or [])
            lines.append(f"- memory:{item_id}｜{item.get('memory_type')}｜sources={source_count}")
            memory_count += 1
        else:
            if note_count >= note_limit:
                continue
            title = item.get("title") or item_id
            time = item.get("time") or item.get("ts") or ""
            lines.append(f"- note:{item_id}｜{title}｜{str(time)[:10]}")
            note_count += 1
    return lines


def _with_sources(answer: str, selected_evidence: Any) -> str:
    """函数功能：`_with_sources` 负责处理 with sources，服务于本文件职责：问答主编排。
    传参：
        answer: answer 参数，由调用方传入，类型为 `str`。
        selected_evidence: selected evidence 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    sources = _source_lines(selected_evidence)
    if not sources:
        return answer
    return answer.rstrip() + "\n\n来源（最多展示 5 条记忆和 5 条笔记）：\n" + "\n".join(sources)


def _build_evidence_bundle(selected_evidence: Any, observations: list[dict[str, Any]] | None = None):
    """Build a structured evidence bundle from real runtime evidence only."""
    from agent.answer_models import EvidenceBundle, RetrievalEvidence

    selected_ids = set(_result_ids(selected_evidence))
    items: list[RetrievalEvidence] = []
    seen: set[str] = set()

    def add_item(item: dict[str, Any], *, tool: str | None = None, selected: bool = False, rank: int | None = None) -> None:
        item_id = str(item.get("id") or item.get("memory_id") or item.get("note_id") or "")
        if not item_id or item_id in seen:
            return
        seen.add(item_id)
        source_ids: list[str] = []
        # A timeline version is supported by its own source, not every source
        # on the parent memory. This preserves per-version provenance.
        if item.get("history") and item.get("source_note_id"):
            source_ids.append(str(item.get("source_note_id")))
        else:
            for source in item.get("sources") or []:
                if isinstance(source, dict):
                    note_id = str(source.get("note_id") or source.get("source_ref") or "")
                    if note_id:
                        source_ids.append(note_id)
                elif source:
                    source_ids.append(str(source))
            if item.get("source_note_id"):
                source_ids.append(str(item.get("source_note_id")))
        if item.get("selected_source_ids"):
            source_ids.extend(str(source_id) for source_id in item.get("selected_source_ids") if source_id)
        kind = "memory"
        if item.get("history"):
            kind = "version"
        elif not item.get("memory_type") and (item.get("title") or item.get("text") or item.get("ts")):
            kind = "source"
        role = "candidate"
        status = str(item.get("status") or "")
        if status in {"superseded", "expired", "archived", "forgotten"}:
            role = "stale_history"
        elif status == "conflicted":
            role = "conflict"
        elif status in {"pending_review", "pending"}:
            role = "candidate"
        elif item.get("history"):
            role = "history"
        else:
            role = "current"
        evidence = RetrievalEvidence(
            kind=kind,
            id=item_id,
            memory_id=str(item.get("memory_id") or item.get("id") or "") or None,
            version_id=str(item.get("version_id") or item.get("id") or "") if kind == "version" else None,
            source_ids=source_ids,
            memory_type=str(item.get("memory_type") or "") or None,
            status=status or None,
            task_status=str(item.get("task_status") or "") or None,
            score=float(item.get("score") or item.get("retrieval_fusion_score") or 0.0) if item.get("score") is not None or item.get("retrieval_fusion_score") is not None else None,
            rank=rank,
            channel=str(item.get("retrieval_channel") or item.get("channel") or "") or None,
            tool=tool,
            selected=selected or item_id in selected_ids,
            role=role,
            metadata={k: v for k, v in item.items() if k not in {"sources"}},
        )
        items.append(evidence)

    observations = observations or []
    for observation in observations:
        tool = str(observation.get("tool") or "") or None
        result = observation.get("result")
        if isinstance(result, list):
            for rank, item in enumerate(result, start=1):
                if isinstance(item, dict):
                    add_item(item, tool=tool, selected=str(item.get("id") or "") in selected_ids, rank=rank)
        elif isinstance(result, dict):
            for rank, item in enumerate(_evidence_items(result), start=1):
                add_item(item, tool=tool, selected=str(item.get("id") or item.get("memory_id") or "") in selected_ids, rank=rank)

    if isinstance(selected_evidence, list):
        for item in selected_evidence:
            if isinstance(item, dict):
                add_item(item, selected=True)
    elif isinstance(selected_evidence, dict):
        for item in _evidence_items(selected_evidence):
            add_item(item, selected=True)

    bundle = EvidenceBundle(items=items)
    if not bundle.selected_context_refs:
        bundle.selected_context_refs = list(dict.fromkeys(bundle.selected_memory_ids + bundle.selected_version_ids))
    return bundle


def _store_answer_evidence(
    hook_context: AgentRunContext | None,
    *,
    answer_source: str,
    selected_evidence: Any,
    observations: list[dict[str, Any]],
) -> None:
    bundle = _build_evidence_bundle(selected_evidence, observations)
    payload = bundle.to_dict()
    payload["answer_source"] = answer_source
    if hook_context is not None:
        hook_context.metadata["answer_evidence_bundle"] = payload
        hook_context.metadata["selected_context_refs"] = list(bundle.selected_context_refs)
        hook_context.metadata["selected_tool_refs"] = list(bundle.selected_tool_refs)
        hook_context.metadata["executed_tools"] = list(bundle.executed_tools)


def _log_final_answer(
    space_id: str,
    answer: str,
    *,
    source: str,
    observations: list[dict[str, Any]] | None = None,
) -> None:
    """函数功能：`_log_final_answer` 负责记录日志 final answer，服务于本文件职责：问答主编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        answer: answer 参数，由调用方传入，类型为 `str`。
        source: source 参数，由调用方传入，类型为 `str`。
        observations: observations 参数，由调用方传入，类型为 `list[dict[str, Any]] | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    log_event(
        "query.final_answer",
        space_id=space_id,
        extra={
            "source": source,
            "answer_len": len(answer),
            "observation_count": len(observations or []),
        },
    )


def _note_brief(note: dict[str, Any], *, text_limit: int = 500) -> dict[str, Any]:
    """函数功能：`_note_brief` 负责处理 note brief，服务于本文件职责：问答主编排。
    传参：
        note: note 参数，由调用方传入，类型为 `dict[str, Any]`。
        text_limit: text limit 参数，由调用方传入，类型为 `int`，默认值为 `500`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    return {
        "id": note.get("id"),
        "time": note.get("ts"),
        "title": note.get("title"),
        "type": note.get("type"),
        "tags": note.get("tags", []),
        "summary": note.get("summary"),
        "text": _clip(note.get("text"), text_limit),
        "related": note.get("related", []),
        "enrichment_status": note.get("enrichment_status", "ready"),
    }


def _safe_notes(space_id: str) -> list[dict[str, Any]]:
    """函数功能：`_safe_notes` 负责处理 safe notes，服务于本文件职责：问答主编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    return [note for note in load_index(space_id) if is_note_queryable(note)]


def _find_note(space_id: str, note_id: str) -> dict[str, Any] | None:
    """函数功能：`_find_note` 负责查找 note，服务于本文件职责：问答主编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
    返回结果说明：
        返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
    """
    for note in _safe_notes(space_id):
        if note.get("id") == note_id:
            return note
    return None


def filter_notes(
    space_id: str,
    note_type: str | None = None,
    tags: list[str] | None = None,
    *,
    match_all_tags: bool = True,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """函数功能：`filter_notes` 负责过滤 notes，服务于本文件职责：问答主编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_type: note type 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        tags: tags 参数，由调用方传入，类型为 `list[str] | None`，默认值为 `None`。
        match_all_tags: match all tags 参数，由调用方传入，类型为 `bool`，默认值为 `True`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `30`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    limit = max(1, min(int(limit), 100))
    query_type = str(note_type or "").strip()
    query_tags = [normalize_tag(tag) for tag in (tags or []) if normalize_tag(tag)]

    if query_type:
        if not is_valid_type(query_type):
            return []
        query_type = normalize_type(query_type)

    if query_tags and not all(is_valid_tag(tag) for tag in query_tags):
        return []

    if STORAGE_BACKEND == "postgres":
        if query_tags:
            results = _postgres_query_notes_by_tags(
                space_id,
                query_tags,
                note_type=query_type or None,
                match_all_tags=match_all_tags,
                limit=limit,
            )
        elif query_type:
            results = _postgres_query_notes_by_type(space_id, query_type, limit=limit)
        else:
            results = _postgres_list_recent_notes(
                space_id,
                created_after=datetime(1970, 1, 1).astimezone(),
                limit=limit,
            )
        return [_note_brief(note) for note in results]

    results = []
    for note in _safe_notes(space_id):
        if query_type and note.get("type") != query_type:
            continue

        note_tags = set(note.get("tags", []))
        if query_tags:
            wanted = set(query_tags)
            if match_all_tags and not wanted.issubset(note_tags):
                continue
            if not match_all_tags and not wanted.intersection(note_tags):
                continue

        results.append(note)

    results.sort(key=lambda item: item.get("ts", ""), reverse=True)
    return [_note_brief(note) for note in results[:limit]]


def by_type(space_id: str, note_type: str, limit: int = 30) -> list[dict[str, Any]]:
    """函数功能：`by_type` 负责处理 by type，服务于本文件职责：问答主编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_type: note type 参数，由调用方传入，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `30`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    return filter_notes(
        space_id,
        note_type=note_type,
        limit=limit,
    )


def by_tag(space_id: str, tag: str, limit: int = 10) -> list[dict[str, Any]]:
    """函数功能：`by_tag` 负责处理 by tag，服务于本文件职责：问答主编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        tag: tag 参数，由调用方传入，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `10`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    return filter_notes(
        space_id,
        tags=[tag],
        match_all_tags=True,
        limit=limit,
    )


def semantic_search(
    space_id: str,
    query: str,
    top_k: int = QUERY_TOP_K,
    min_score: float = DEFAULT_QUERY_MIN_SCORE,
) -> list[dict[str, Any]]:
    """函数功能：`semantic_search` 负责搜索 semantic，服务于本文件职责：问答主编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        query: 检索或查询文本，类型为 `str`。
        top_k: top k 参数，由调用方传入，类型为 `int`，默认值为 `QUERY_TOP_K`。
        min_score: min score 参数，由调用方传入，类型为 `float`，默认值为 `DEFAULT_QUERY_MIN_SCORE`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    query = query.strip()
    if not query:
        return []

    top_k = max(1, min(int(top_k), 10))
    min_score = float(min_score)
    if STORAGE_BACKEND == "postgres" and settings.NOTE_HYBRID_RETRIEVAL_ENABLED:
        # Note 向量回填期间，稠密召回是可选路径；embedding provider 不可用或 Note 尚无向量时，混合检索仍会返回精确/稀疏证据。
        embedding = None
        if settings.NOTE_HYBRID_VECTOR_ENABLED:
            try:
                embedding = embed_text(query)
            except Exception:
                embedding = None
        return [
            _note_brief(note) | {
                "score": note.get("score", 0.0),
                "retrieval_channels": note.get("retrieval_channels", []),
            }
            for note in _postgres_hybrid_search_notes(
                space_id,
                query,
                query_embedding=embedding,
                limit=top_k,
            )
        ]
    embedding = embed_text(query)
    results = search_related(
        space_id,
        embedding,
        top_k=top_k,
        min_score=min_score,
    )

    return [
        {
            "id": result.note_id,
            "message_id": result.message_id,
            "score": round(result.score, 4),
            "title": result.metadata.get("title"),
            "type": result.metadata.get("type"),
            "tags": result.metadata.get("tags", []),
            "summary": result.metadata.get("summary"),
            "time": result.metadata.get("ts"),
            "text": _clip(result.text),
        }
        for result in results
    ]


_QUERY_FILLERS = (
    "请问",
    "帮我",
    "告诉我",
    "查一下",
    "看一下",
    "什么",
    "哪个",
    "哪些",
    "是否",
    "有没有",
    "相关内容",
    "相关记录",
    "刚才",
    "上次",
)


def _lexical_terms(text: str) -> set[str]:
    """函数功能：`_lexical_terms` 负责处理 lexical terms，服务于本文件职责：问答主编排。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `set[str]` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    value = str(text or "").casefold()
    for filler in _QUERY_FILLERS:
        value = value.replace(filler, "")
    latin = set(re.findall(r"[a-z0-9][a-z0-9+#._-]*", value))
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", value)
    terms = set(latin)
    for run in cjk_runs:
        if len(run) == 1:
            terms.add(run)
        else:
            terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return {term for term in terms if term}


def provisional_search(space_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
    """函数功能：`provisional_search` 负责搜索 provisional，服务于本文件职责：问答主编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        query: 检索或查询文本，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `5`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    query_terms = _lexical_terms(query)
    if not query_terms:
        return []

    candidates = (
        _postgres_list_provisional_notes(space_id, limit=max(100, min(int(limit) * 20, 500)))
        if STORAGE_BACKEND == "postgres"
        else _safe_notes(space_id)
    )
    scored: list[tuple[float, dict[str, Any]]] = []
    for note in candidates:
        status = str(note.get("enrichment_status") or "ready")
        if status not in {"provisional", "enriching", "failed"}:
            continue
        note_terms = _lexical_terms(
            " ".join(
                (
                    str(note.get("title") or ""),
                    str(note.get("summary") or ""),
                    str(note.get("text") or ""),
                )
            )
        )
        overlap = len(query_terms & note_terms) / max(1, len(query_terms))
        if overlap < 0.34:
            continue
        scored.append((overlap, note))

    scored.sort(key=lambda item: (item[0], str(item[1].get("ts") or "")), reverse=True)
    return [
        {**_note_brief(note), "score": round(score, 4), "provisional": True}
        for score, note in scored[: max(1, min(int(limit), 10))]
    ]


def memory_note_fallback(
    space_id: str,
    query: str,
    *,
    limit: int = QUERY_TOP_K,
    min_score: float = DEFAULT_QUERY_MIN_SCORE,
) -> list[dict[str, Any]]:
    """函数功能：`memory_note_fallback` 负责处理 memory note fallback，服务于本文件职责：问答主编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        query: 检索或查询文本，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `QUERY_TOP_K`。
        min_score: min score 参数，由调用方传入，类型为 `float`，默认值为 `DEFAULT_QUERY_MIN_SCORE`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    if STORAGE_BACKEND == "postgres":
        if settings.NOTE_HYBRID_RETRIEVAL_ENABLED:
            return semantic_search(space_id, query, top_k=limit, min_score=min_score)
        return [_note_brief(note) for note in _postgres_search_notes_memory_fallback(space_id, query, limit=limit)]
    return semantic_search(space_id, query, top_k=limit, min_score=min_score)


def get_note(space_id: str, note_id: str) -> dict[str, Any]:
    """函数功能：`get_note` 负责获取 note，服务于本文件职责：问答主编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    note = _postgres_find_note(space_id, note_id) if STORAGE_BACKEND == "postgres" else _find_note(space_id, note_id)
    if note is not None and not is_note_queryable(note):
        note = None
    if note is None:
        return {"error": f"note not found: {note_id}"}
    return _note_brief(note, text_limit=1200)


def list_recent(space_id: str, days: int = 7, limit: int = 10) -> list[dict[str, Any]]:
    """函数功能：`list_recent` 负责列出 recent，服务于本文件职责：问答主编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        days: days 参数，由调用方传入，类型为 `int`，默认值为 `7`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `10`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 30))

    now = datetime.now().astimezone()
    cutoff = now - timedelta(days=days)

    if STORAGE_BACKEND == "postgres":
        return [_note_brief(note) for note in _postgres_list_recent_notes(space_id, created_after=cutoff, limit=limit)]

    notes = []
    for note in _safe_notes(space_id):
        ts = _parse_ts(note.get("ts"))
        if ts is not None and ts >= cutoff:
            notes.append(note)

    notes.sort(key=lambda item: item.get("ts", ""), reverse=True)
    return [_note_brief(note) for note in notes[:limit]]


def follow_links(space_id: str, note_id: str, limit: int = 5) -> dict[str, Any]:
    """函数功能：`follow_links` 负责处理 follow links，服务于本文件职责：问答主编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `5`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    limit = max(1, min(int(limit), 20))
    if STORAGE_BACKEND == "postgres":
        relations = _postgres_get_note_relations(space_id, note_id, limit=limit)
        if relations is None or relations.get("source") is None:
            return {"error": f"note not found: {note_id}"}
        source = _note_brief(relations["source"])
        outbound = [_note_brief(note) for note in relations["outbound"]]
        inbound = [_note_brief(note) for note in relations["inbound"]]
        return {
            "source": source,
            "outbound_related": outbound,
            "inbound_related": inbound,
            "related": outbound + inbound,
        }
    notes = _safe_notes(space_id)
    note = next((item for item in notes if item.get("id") == note_id), None)
    if note is None:
        return {"error": f"note not found: {note_id}"}

    notes_by_id = {
        item.get("id"): item
        for item in notes
        if item.get("id")
    }

    outbound = []
    for related_id in note.get("related", [])[:limit]:
        related_note = notes_by_id.get(related_id)
        if related_note is not None:
            outbound.append(_note_brief(related_note))

    inbound = []
    for item in notes:
        if item.get("id") == note_id:
            continue
        if note_id in item.get("related", []):
            inbound.append(_note_brief(item))
        if len(inbound) >= limit:
            break

    return {
        "source": _note_brief(note),
        "outbound_related": outbound,
        "inbound_related": inbound,
        "related": outbound + inbound,
    }


def related_notes(
    space_id: str,
    query: str,
    top_k: int = 3,
    min_score: float = DEFAULT_QUERY_MIN_SCORE,
    limit: int = 5,
) -> dict[str, Any]:
    """函数功能：`related_notes` 负责处理 related notes，服务于本文件职责：问答主编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        query: 检索或查询文本，类型为 `str`。
        top_k: top k 参数，由调用方传入，类型为 `int`，默认值为 `3`。
        min_score: min score 参数，由调用方传入，类型为 `float`，默认值为 `DEFAULT_QUERY_MIN_SCORE`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `5`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    candidates = semantic_search(
        space_id,
        query,
        top_k=top_k,
        min_score=min_score,
    )
    if not candidates:
        return {
            "query": query,
            "candidates": [],
            "related_groups": [],
        }

    related_groups = []
    for candidate in candidates:
        note_id = candidate.get("id")
        if not note_id:
            continue
        related_groups.append(follow_links(space_id, str(note_id), limit=limit))

    return {
        "query": query,
        "candidates": candidates,
        "related_groups": related_groups,
    }


def _memory_access_context(value: Any = None, *, requester: str | None = None) -> dict[str, Any] | None:
    if value is None and requester is None:
        return None
    if isinstance(value, AccessContext):
        return {"requester": value.requester, "owner_id": value.owner_id, "allow_sensitive": value.allow_sensitive, "allow_restricted": value.allow_restricted}
    if isinstance(value, dict):
        data = dict(value)
        data.setdefault("requester", requester or "owner")
        data.setdefault("owner_id", "owner")
    else:
        data = {"requester": requester or "owner", "owner_id": requester or "owner"}
    return data


def _memory_search_compat(space_id: str, query: str, **kwargs: Any) -> list[dict[str, Any]]:
    try:
        return memory_search(space_id, query, **kwargs)
    except TypeError as exc:
        if "access_context" not in str(exc):
            raise
        kwargs.pop("access_context", None)
        return memory_search(space_id, query, **kwargs)


def _query_current_intent(question: str) -> bool:
    normalized = _normalized_query(question)
    return any(marker in normalized for marker in ("现在", "当前", "目前", "到底", "还算", "focus", "主要", "重点"))


def _query_history_intent(question: str, route: dict[str, Any] | None = None) -> bool:
    if route and route.get("action") == "memory_history":
        return True
    normalized = _normalized_query(question)
    return any(marker in normalized for marker in ("历史", "之前", "以前", "最初", "变化", "变成", "经历", "完成前", "过去", "版本"))


def _query_ambiguous_reference(question: str) -> bool:
    normalized = _normalized_query(question)
    return any(marker in normalized for marker in ("那个", "这个", "那件", "这件", "它", "哪个", "哪一个", "哪条", "哪种"))


def _query_conflict_intent(question: str) -> bool:
    normalized = _normalized_query(question)
    return any(marker in normalized for marker in ("到底", "冲突", "矛盾", "不一致", "确认一下", "究竟"))


def _parse_list_limit(question: str, default: int = 3) -> int:
    normalized = _normalized_query(question)
    explicit = re.search(r"(\d+)\s*(?:个|项|条|件|份)?", normalized)
    if explicit:
        return max(1, min(int(explicit.group(1)), 10))
    numeral_map = {
        "一": 1,
        "两": 2,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    for token, value in numeral_map.items():
        if token in normalized and any(marker in normalized for marker in ("个", "项", "条", "件", "份", "项目", "状态")):
            return value
    return max(1, min(int(default), 10))


def parse_list_constraints(question: str, *, default_limit: int = 3) -> dict[str, Any]:
    """Parse portable list constraints; never infer them from evaluation data."""
    normalized = _normalized_query(question)
    memory_type = "profile"
    if any(marker in normalized for marker in ("经历", "最近几件", "事件")):
        memory_type = "episodic"
    elif any(marker in normalized for marker in ("任务", "待办", "进度", "状态", "项目")):
        memory_type = "task"

    status: str | None = None
    for token, value in (
        ("todo", "todo"),
        ("待办", "todo"),
        ("待处理", "todo"),
        ("blocked", "blocked"),
        ("阻塞", "blocked"),
        ("done", "done"),
        ("完成", "done"),
        ("canceled", "canceled"),
        ("cancelled", "canceled"),
        ("取消", "canceled"),
    ):
        if token in normalized:
            status = value
            break
    return {
        "limit": _parse_list_limit(normalized, default=default_limit),
        "memory_type": memory_type,
        "status": status,
        "recent": "最近" in normalized,
    }


def _time_value(value: Any) -> str:
    parsed = _parse_ts(value if isinstance(value, str) else str(value or ""))
    return parsed.isoformat() if parsed is not None else ""


def _business_time_value(item: dict[str, Any]) -> str:
    return next((value for value in _business_time_sort_key(item) if value), "")


def _business_time_sort_key(item: dict[str, Any]) -> tuple[str, ...]:
    """Return business-time fields in priority order for stable comparisons."""
    scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
    event_candidates = (
        item.get("event_time"),
        item.get("event_at"),
        item.get("business_time"),
        scope.get("event_time"),
        scope.get("event_at"),
    )
    valid_candidates = (item.get("valid_from"), scope.get("valid_from"))
    content_date = re.search(r"20\d{2}-\d{1,2}-\d{1,2}", str(item.get("content") or ""))
    sources = item.get("sources") if isinstance(item.get("sources"), list) else []
    source_event_candidates: list[Any] = []
    source_observed_candidates: list[Any] = []
    for source in sources:
        if isinstance(source, dict):
            source_event_candidates.append(source.get("event_time"))
            source_observed_candidates.append(source.get("observed_at"))

    def latest(values: tuple[Any, ...] | list[Any]) -> str:
        return max((_time_value(value) for value in values if _time_value(value)), default="")

    # A tie in a higher-priority field must be resolved by the next field. For
    # example, equal ingestion valid_from values must not hide different event
    # dates stated in the episodic content.
    return (
        latest(event_candidates),
        latest(valid_candidates),
        _time_value(content_date.group(0)) if content_date else "",
        latest(source_event_candidates),
        latest(source_observed_candidates),
        _time_value(item.get("updated_at")),
        _time_value(item.get("created_at")),
    )


def _item_completeness_score(item: dict[str, Any]) -> int:
    score = 0
    if str(item.get("memory_type") or ""):
        score += 1
    if str(item.get("canonical_topic") or item.get("memory_key") or item.get("object_value") or ""):
        score += 1
    if str(item.get("task_status") or item.get("status") or ""):
        score += 1
    if str(item.get("current_value") or ""):
        score += 2
    if str(item.get("content") or ""):
        score += 1
    if item.get("sources"):
        score += 1
    if item.get("versions"):
        score += 1
    return score


def _item_business_score(item: dict[str, Any]) -> tuple[Any, ...]:
    content = str(item.get("content") or "")
    current_value = str(item.get("current_value") or "")
    task_status = str(item.get("task_status") or "")
    scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
    canonical_topic = str(scope.get("canonical_topic") or item.get("canonical_topic") or item.get("memory_key") or item.get("object_value") or item.get("content") or "")
    source_count = len(item.get("sources") or [])
    source_text = " ".join(
        str(source.get("evidence_text") or source.get("content") or "")
        for source in (item.get("sources") or [])
        if isinstance(source, dict)
    )
    status_signal_text = " ".join((content, source_text, current_value, str(item.get("object_value") or "")))
    structured_status_signal = bool(
        re.search(
            r"(?:[:：=]|当前(?:状态)?(?:是|为)?|现在(?:状态)?(?:是|为)?)\s*"
            r"(todo|blocked|done|canceled|cancelled|pending_review|pending)\b",
            status_signal_text,
            flags=re.IGNORECASE,
        )
    )
    state_matches_record = bool(task_status) and str(item.get("object_value") or current_value or "").casefold() == task_status.casefold()
    current_signal = 1 if current_value or structured_status_signal or state_matches_record else 0
    explicit_current_signal = 1 if any(marker in content for marker in ("当前", "现在")) or any(marker in source_text for marker in ("当前", "现在", "是todo", "是blocked", "是done")) or current_value or structured_status_signal or state_matches_record else 0
    irrelevant_penalty = 1 if any(marker in source_text for marker in ("无关信息", "无关")) or "无关" in content else 0
    duplicate_signal = 1 if canonical_topic and ((content and canonical_topic in content) or (source_text and canonical_topic in source_text)) else 0
    completeness = _item_completeness_score(item)
    business_time = _business_time_value(item)
    return (
        current_signal,
        explicit_current_signal,
        completeness,
        source_count,
        1 - irrelevant_penalty,
        duplicate_signal,
        {
            "todo": 5,
            "blocked": 4,
            "done": 3,
            "canceled": 2,
            "cancelled": 2,
            "pending_review": 1,
            "pending": 1,
            "active": 0,
        }.get(task_status, 0),
        business_time,
        str(item.get("updated_at") or ""),
    )


def _item_identity_key(item: dict[str, Any]) -> str:
    scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
    return str(
        scope.get("canonical_topic")
        or item.get("canonical_topic")
        or item.get("memory_key")
        or item.get("object_value")
        or item.get("id")
        or item.get("content")
        or ""
    )


def _sorted_business_items(items: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    best_by_key: dict[str, dict[str, Any]] = {}
    best_score_by_key: dict[str, tuple[Any, ...]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        key = _item_identity_key(item)
        score = _item_business_score(item)
        if key not in best_by_key or score > best_score_by_key[key]:
            best_by_key[key] = item
            best_score_by_key[key] = score
    # Stable tie-breaker is id ascending; all business fields above remain
    # descending, so repository return order never changes a list answer.
    ordered = sorted(best_by_key.values(), key=lambda item: str(item.get("id") or ""))
    ordered.sort(key=_item_business_score, reverse=True)
    return ordered[: max(1, min(int(limit), 100))]


def _evidence_identity_key(item: dict[str, Any]) -> str:
    key = str(item.get("memory_key") or "").strip()
    if key:
        return key
    memory_type = str(item.get("memory_type") or "")
    subject = str(item.get("subject") or "")
    predicate = str(item.get("predicate") or "")
    topic = str((item.get("scope") or {}).get("canonical_topic") or item.get("object_value") or item.get("content") or "")
    return "|".join(part for part in (memory_type, subject, predicate, topic) if part)


def _item_query_overlap(question: str, item: dict[str, Any]) -> float:
    text = " ".join(
        str(item.get(key) or "")
        for key in ("content", "subject", "predicate", "object_value", "memory_key", "task_status", "memory_type")
    )
    q_terms = _lexical_terms(question)
    c_terms = _lexical_terms(text)
    if not q_terms or not c_terms:
        return 0.0
    return len(q_terms & c_terms) / len(q_terms)


def _query_requested_attribute(question: str) -> str | None:
    """Extract a user-facing preference attribute without relying on case data."""
    normalized = _normalized_query(question)
    patterns = (
        r"(?:最喜欢|最愛|偏好|喜欢|喜歡|讨厌|討厭|不喜欢|不喜歡)(?:的)?\s*([^？?，。吗呢是]+?)(?:是什么|是什麼|是什么呢|嗎|吗|呢|？|\?|$)",
        r"(?:关于|關於)\s*([^？?，。]+?)(?:的)?(?:偏好|喜好|喜欢|喜歡)",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if not match:
            continue
        value = match.group(1).strip(" 的是")
        value = re.sub(r"^(?:我|你|用户|使用者)(?:最)?(?:喜欢|喜歡|偏好|讨厌|討厭|不喜欢|不喜歡)(?:的)?", "", value)
        value = value.strip(" 的是")
        if value in {"什么", "什麼", "哪个", "哪個", "哪些"}:
            continue
        if value:
            return value
    return None


def _candidate_topic_text(item: dict[str, Any]) -> str:
    scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
    return " ".join(
        str(value or "")
        for value in (
            item.get("attribute"),
            item.get("predicate"),
            item.get("canonical_topic"),
            scope.get("canonical_topic"),
            item.get("object_value"),
            item.get("content"),
            item.get("memory_key"),
        )
    )


def _topic_compatible(question: str, item: dict[str, Any]) -> bool:
    """Require the requested attribute to be supported, not merely memory type."""
    requested = _query_requested_attribute(question)
    if not requested:
        return True
    candidate_text = _candidate_topic_text(item)
    requested_norm = _normalized_query(requested).replace(" ", "")
    candidate_norm = _normalized_query(candidate_text).replace(" ", "")
    if requested_norm and requested_norm in candidate_norm:
        return True
    requested_terms = _lexical_terms(requested)
    candidate_terms = _lexical_terms(candidate_text)
    # For one-character Chinese attributes only literal containment is safe.
    if len(requested_norm) <= 1:
        return bool(requested_norm and requested_norm in candidate_norm)
    return bool(requested_terms and len(requested_terms & candidate_terms) / len(requested_terms) >= 0.72)


def _relevant_evidence_items(question: str, evidence: list[dict[str, Any]], *, floor: float = 0.70) -> list[dict[str, Any]]:
    """Keep high-confidence evidence. This avoids answering absent-topic asks from weak lexical overlap."""
    if not evidence:
        return []
    ranked = sorted(
        [item for item in evidence if isinstance(item, dict)],
        key=lambda item: (float(item.get("score") or item.get("retrieval_fusion_score") or 0.0), _item_query_overlap(question, item)),
        reverse=True,
    )
    if not ranked:
        return []
    top_score = float(ranked[0].get("score") or ranked[0].get("retrieval_fusion_score") or 0.0)
    kept: list[dict[str, Any]] = []
    for item in ranked:
        if settings.QUERY_TOPIC_COMPATIBILITY_GATE_ENABLED and not _topic_compatible(question, item):
            continue
        score = float(item.get("score") or item.get("retrieval_fusion_score") or 0.0)
        overlap = _item_query_overlap(question, item)
        content = str(item.get("content") or item.get("object_value") or "")
        current_focus_hit = (
            _query_current_intent(question)
            and str(item.get("memory_type") or "") == "semantic"
            and score >= 0.45
            and any(marker in content for marker in ("当前", "现在", "重点", "主要", "focus"))
        )
        ambiguous_reference_hit = _query_ambiguous_reference(question) and score >= 0.45 and overlap >= 0.08
        if (
            score >= floor
            or (floor <= 0.0 and overlap >= 0.12)
            or (top_score >= 0.90 and score >= max(0.45, top_score - 0.25))
            or (score >= 0.45 and overlap >= 0.20)
            or current_focus_hit
            or ambiguous_reference_hit
        ):
            kept.append(item)
    return kept


def _selected_evidence_answer(item: dict[str, Any], *, prefix: str = "根据记忆") -> str:
    content = str(item.get("content") or item.get("summary") or item.get("object_value") or "").strip()
    if not content:
        return "我找到了相关记忆，但缺少可直接展示的内容。"
    return f"{prefix}，{content}。"


def _supported_claims_from_bundle(bundle: Any, *, answer_type: str, reason_code: str) -> list[Any]:
    """Produce one factual claim per selected production evidence item."""
    from agent.answer_models import SupportedClaim

    if answer_type not in {"answered", "qualified_history_only"} or bundle is None:
        return []
    claims: list[SupportedClaim] = []
    seen: set[tuple[str, str, str]] = set()
    for item in getattr(bundle, "items", []) or []:
        if not getattr(item, "selected", False) or getattr(item, "kind", "") == "access_denied":
            continue
        metadata = getattr(item, "metadata", {}) or {}
        text = str(metadata.get("content") or metadata.get("summary") or metadata.get("object_value") or "").strip()
        if not text:
            continue
        memory_id = str(getattr(item, "memory_id", "") or "")
        version_id = str(getattr(item, "version_id", "") or "")
        key = (text, memory_id, version_id)
        if key in seen:
            continue
        seen.add(key)
        role = "history" if getattr(item, "role", "") in {"history", "stale_history"} or reason_code in {"history_query", "stale_history_only"} else "current"
        claims.append(
            SupportedClaim(
                text=text,
                claim_id=(f"version:{version_id}" if version_id else f"memory:{memory_id}"),
                claim_type="history_fact" if role == "history" else "fact",
                memory_ids=[memory_id] if memory_id else [],
                version_ids=[version_id] if version_id else [],
                source_ids=list(getattr(item, "source_ids", []) or []),
                support_role=role,
                confidence=getattr(item, "score", None),
            )
        )
    return claims


def _timeline_claim_groups(
    bundle: Any,
    claims: list[Any],
    *,
    answer_type: str,
    reason_code: str,
    answer: str,
) -> list[Any]:
    """Expose a summary of a versioned timeline without discarding atomic claims."""
    from agent.answer_models import ClaimGroup, SupportedClaim

    if not settings.QUERY_TIMELINE_CLAIM_GROUP_ENABLED:
        return []
    if answer_type not in {"answered", "qualified_history_only"} or reason_code not in {"history_query", "stale_history_only"}:
        return []
    history_claims = [
        claim for claim in claims
        if getattr(claim, "support_role", "") == "history" and getattr(claim, "version_ids", None)
    ]
    if len(history_claims) < 2:
        return []
    order_by_version: dict[str, tuple[int, str]] = {}
    for item in getattr(bundle, "items", []) or []:
        version_id = str(getattr(item, "version_id", "") or "")
        if not version_id:
            continue
        metadata = getattr(item, "metadata", {}) or {}
        order_by_version[version_id] = (
            int(metadata.get("version") or metadata.get("sequence") or 0),
            str(metadata.get("valid_from") or metadata.get("created_at") or version_id),
        )
    history_claims.sort(
        key=lambda claim: order_by_version.get(str(claim.version_ids[0]), (0, str(claim.version_ids[0])))
    )
    memory_ids = list(dict.fromkeys(memory_id for claim in history_claims for memory_id in claim.memory_ids))
    version_ids = list(dict.fromkeys(version_id for claim in history_claims for version_id in claim.version_ids))
    source_ids = list(dict.fromkeys(source_id for claim in history_claims for source_id in claim.source_ids))
    summary_text = str(answer or "").split("来源（", 1)[0].strip()
    if not summary_text:
        return []
    summary = SupportedClaim(
        text=summary_text,
        claim_id="timeline_summary:" + ":".join(version_ids),
        claim_type="timeline_summary",
        memory_ids=memory_ids,
        version_ids=version_ids,
        source_ids=source_ids,
        support_role="history",
    )
    return [
        ClaimGroup(
            group_type="timeline",
            summary_claim=summary,
            ordered_member_claim_ids=[str(claim.claim_id) for claim in history_claims if claim.claim_id],
            member_claims=history_claims,
            memory_ids=memory_ids,
            version_ids=version_ids,
            source_ids=source_ids,
            support_role="history",
        )
    ]


def _conflict_answer(items: list[dict[str, Any]]) -> str:
    # Conflict answers must not repeat either side as a fact.  Expose only a
    # readable business topic and the need for confirmation.
    first = next((item for item in items if isinstance(item, dict)), {})
    scope = first.get("scope") if isinstance(first.get("scope"), dict) else {}
    topic = str(first.get("predicate") or scope.get("canonical_topic") or first.get("memory_type") or "该事项")
    return f"关于“{topic}”存在相互冲突或尚待确认的记录，暂不能确认当前结论。"


def _clarification_answer(items: list[dict[str, Any]]) -> str:
    lines = ["我找到多个可能对象，无法安全判断你指的是哪一个；请补充具体主题或阶段："]
    for item in items[:5]:
        content = str(item.get("content") or item.get("object_value") or item.get("id") or "")
        lines.append(f"- {content}")
    return "\n".join(lines)


def _stale_history_fallback(space_id: str, question: str, *, limit: int = 8, access_context: Any = None) -> list[dict[str, Any]]:
    """Read-only stale/history fallback for current questions with no active answer."""
    try:
        candidates = list_memories(space_id, status=None, limit=max(1, min(int(limit), 100)))
    except TypeError:
        candidates = list_memories(space_id, status=None, limit=max(1, min(int(limit), 100)))  # type: ignore[misc]
    if access_context is not None:
        from memory.access import memory_access_allowed
        candidates = [memory for memory in candidates if memory_access_allowed(memory, access_context)]
    # `MemoryRecord.to_dict()` deliberately does not hydrate versions. Fetch the
    # first-class timeline once, otherwise stale-only answers lose their real
    # version ID and per-version source provenance.
    timelines = {
        str(item.get("memory_id") or item.get("id") or ""): item
        for item in get_memory_timeline(space_id, limit=max(1, min(int(limit), 100)), access_context=access_context)
        if isinstance(item, dict)
    }
    rows: list[dict[str, Any]] = []
    for memory in candidates:
        status = str(getattr(memory, "status", "") or "")
        if status not in {"superseded", "expired", "archived", "forgotten", "deleted"}:
            continue
        data = dict(timelines.get(str(getattr(memory, "id", ""))) or memory.to_dict())
        overlap = _item_query_overlap(question, data)
        if overlap < 0.08:
            continue
        versions = data.get("versions") or []
        if versions:
            for version in versions:
                item = dict(version)
                item["id"] = item.get("id") or f"{data.get('id')}:v{item.get('version')}"
                item["memory_id"] = data.get("id")
                item["memory_type"] = data.get("memory_type")
                item["memory_key"] = data.get("memory_key")
                item["history"] = True
                item["status"] = status
                # `_build_evidence_bundle` will use source_note_id for a
                # version; retain parent sources only as a rendering fallback.
                item["sources"] = data.get("sources") or []
                item["score"] = max(0.45, overlap)
                rows.append(item)
        else:
            data["history"] = True
            data["score"] = max(0.45, overlap)
            rows.append(data)
    return rows[: max(1, min(int(limit), 20))]


def decide_answer(
    question: str,
    route: dict[str, Any] | None,
    evidence_bundle: Any,
    *,
    current_evidence: list[dict[str, Any]] | None = None,
    history_evidence: list[dict[str, Any]] | None = None,
    restricted_denied: bool = False,
    pending_review_ids: list[str] | None = None,
) -> "AnswerDecision":
    """Evidence-first answer decision. LLM/string text must not decide availability."""
    from agent.answer_models import AnswerDecision, EvidenceBundle

    bundle = EvidenceBundle.from_dict(evidence_bundle) if isinstance(evidence_bundle, dict) else evidence_bundle
    items = list(getattr(bundle, "items", []) or [])
    current_evidence = list(current_evidence or [])
    history_evidence = list(history_evidence or [])
    pending_review_ids = list(dict.fromkeys(str(item) for item in (pending_review_ids or []) if item))
    if restricted_denied or any(getattr(item, "kind", "") == "access_denied" or getattr(item, "role", "") == "access_denied" for item in items):
        return AnswerDecision("restricted", "acl_filtered_all_evidence")
    if not items and not current_evidence and not history_evidence:
        return AnswerDecision("no_answer", "no_relevant_evidence")
    if _query_history_intent(question, route) and (history_evidence or any(getattr(item, "role", "") in {"history", "stale_history"} for item in items)):
        return AnswerDecision("answered", "history_query")
    if _query_conflict_intent(question) and any(str(item.get("status") or "") in {"pending_review", "pending", "conflicted"} for item in current_evidence):
        conflict_ids = pending_review_ids + [
            str(item.get("id") or item.get("memory_id"))
            for item in current_evidence
            if str(item.get("status") or "") in {"pending_review", "pending", "conflicted"}
        ]
        return AnswerDecision("conflict", "pending_review_conflict", conflict_ids=list(dict.fromkeys(conflict_ids)))
    polarities: dict[str, set[str]] = {}
    for item in current_evidence:
        key = _evidence_identity_key(item)
        polarity = str(item.get("polarity") or "")
        if key and polarity:
            polarities.setdefault(key, set()).add(polarity)
    if any(len(values) > 1 for values in polarities.values()):
        return AnswerDecision("conflict", "polarity_conflict")
    if _query_ambiguous_reference(question):
        active_items = [item for item in current_evidence if str(item.get("status") or "active") == "active"]
        identity_count = len({str(item.get("memory_key") or item.get("id") or "") for item in active_items})
        task_like = [item for item in active_items if str(item.get("memory_type") or "") == "task"]
        if identity_count >= 2 and len(task_like) >= 2:
            return AnswerDecision("clarification", "ambiguous_candidates")
    if not current_evidence and history_evidence:
        if _query_current_intent(question):
            return AnswerDecision("qualified_history_only", "stale_history_only")
        return AnswerDecision("answered", "history_query")
    if current_evidence:
        return AnswerDecision("answered", "evidence_supported")
    return AnswerDecision("no_answer", "no_relevant_evidence")


def memory_history(space_id: str, query: str, *, limit: int = 10, access_context: Any = None) -> list[dict[str, Any]]:
    # Use the existing search -> get_memory API pair so this helper works
    # against both SQLite and PostgreSQL repository implementations without a
    # separate timeline-only contract.
    query_terms = _lexical_terms(query)
    hits = _memory_search_compat(space_id, query, min_score=0.0, limit=limit, access_context=access_context)
    timelines: list[dict[str, Any]] = []
    for hit_index, hit in enumerate(hits):
        memory_id = str(hit.get("memory_id") or hit.get("id") or "")
        if not memory_id:
            continue
        hit_terms = _lexical_terms(" ".join(str(hit.get(key) or "") for key in ("content", "subject", "predicate", "object_value", "memory_key")))
        overlap = len(query_terms & hit_terms) / max(1, len(query_terms))
        if hit_index > 0 and query_terms and overlap < 0.12:
            continue
        record = get_memory(memory_id)
        if record is None or str(record.space_id) != str(space_id):
            continue
        if access_context is not None:
            from memory.access import memory_access_allowed
            if not memory_access_allowed(record, access_context):
                continue
        timeline = record.to_dict()
        timeline["_query_overlap"] = overlap
        timeline["_search_score"] = float(hit.get("score") or 0.0)
        timelines.append(timeline)
    versioned = [timeline for timeline in timelines if len(timeline.get("versions") or []) > 1]
    if versioned:
        timelines = versioned
    timelines.sort(
        key=lambda timeline: (
            len(timeline.get("versions") or []),
            float(timeline.get("_query_overlap") or 0.0),
            float(timeline.get("_search_score") or 0.0),
            str(timeline.get("updated_at") or ""),
        ),
        reverse=True,
    )
    if timelines:
        timelines = timelines[:1]
    rows: list[dict[str, Any]] = []
    for timeline in timelines:
        for version in timeline.get("versions") or []:
            item = dict(version)
            item["id"] = item.get("id") or f"{timeline.get('id')}:v{item.get('version')}"
            item["memory_id"] = timeline.get("id")
            item["memory_type"] = timeline.get("memory_type")
            item["memory_key"] = timeline.get("memory_key")
            item["history"] = True
            item["sources"] = timeline.get("sources") or []
            rows.append(item)
    return rows


def list_tasks(space_id: str, *, limit: int = 30, access_context: Any = None, status: str | None = None, query: str = "") -> list[dict[str, Any]]:
    fetch_limit = max(max(1, min(int(limit), 100)) * 4, 30)
    records = list_memories(space_id, status="active", memory_type="task", limit=fetch_limit)
    if access_context is not None:
        from memory.access import memory_access_allowed
        records = [record for record in records if memory_access_allowed(record, access_context)]
    items = [record.to_dict() for record in records]
    if status:
        items = [item for item in items if str(item.get("task_status") or "") == status]
    # `query` is retained as an explicit contract field for future topic/time
    # constraints; sorting itself only uses the returned business fields.
    del query
    return _sorted_business_items(items, limit=limit)


def list_recent_episodes(space_id: str, *, limit: int = 10, access_context: Any = None) -> list[dict[str, Any]]:
    fetch_limit = max(max(1, min(int(limit), 100)) * 4, 30)
    records = list_memories(space_id, status="active", memory_type="episodic", limit=fetch_limit)
    if access_context is not None:
        ctx = AccessContext.from_value(access_context)
        records = [record for record in records if not record.scope.get("sensitivity") or ctx.allow_sensitive or str(ctx.requester or "owner") == str(record.scope.get("owner_id") or ctx.owner_id or "owner")]
    items = [record.to_dict() for record in records]
    items.sort(key=lambda item: str(item.get("id") or ""))
    items.sort(
        key=lambda item: (
            _business_time_sort_key(item),
            _item_completeness_score(item),
            str(item.get("updated_at") or ""),
        ),
        reverse=True,
    )
    return items[: max(1, min(int(limit), 100))]


def profile_summary(space_id: str, query: str, *, limit: int = 20, access_context: Any = None) -> dict[str, Any]:
    slots: dict[str, list[dict[str, Any]]] = {}
    for memory_type in ("preference", "task", "semantic", "episodic"):
        hits = _memory_search_compat(space_id, query, memory_type=memory_type, min_score=0.0, limit=max(1, min(int(limit), 10)), access_context=access_context)
        if hits:
            slots[memory_type] = _sorted_business_items(hits, limit=limit)
    return {"query": query, "slots": slots, "source": "structured_profile_summary"}




def _execute_tool(space_id: str, action: str, args: dict[str, Any], *, access_context: Any = None) -> Any:
    """函数功能：`_execute_tool` 负责执行 tool，服务于本文件职责：问答主编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        action: action 参数，由调用方传入，类型为 `str`。
        args: args 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `Any` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    if action == "by_type":
        return by_type(
            space_id,
            str(args.get("type", args.get("note_type", ""))),
            args.get("limit", 30),
        )
    if action == "by_tag":
        return by_tag(space_id, str(args.get("tag", "")), args.get("limit", 10))
    if action == "filter_notes":
        return filter_notes(
            space_id,
            note_type=args.get("type", args.get("note_type")),
            tags=_coerce_tags(args.get("tags")),
            match_all_tags=_coerce_bool(args.get("match_all_tags"), True),
            limit=args.get("limit", 30),
        )
    if action == "semantic_search":
        return semantic_search(
            space_id,
            str(args.get("query", "")),
            args.get("top_k", QUERY_TOP_K),
            args.get("min_score", DEFAULT_QUERY_MIN_SCORE),
        )
    if action == "get_note":
        return get_note(space_id, str(args.get("note_id", "")))
    if action == "list_recent":
        return list_recent(space_id, args.get("days", 7), args.get("limit", 10))
    if action == "follow_links":
        return follow_links(space_id, str(args.get("note_id", "")), args.get("limit", 5))
    if action == "related_notes":
        return related_notes(
            space_id,
            str(args.get("query", "")),
            args.get("top_k", 3),
            args.get("min_score", DEFAULT_QUERY_MIN_SCORE),
            args.get("limit", 5),
        )
    if action == "memory_search":
        return _memory_search_compat(
            space_id,
            str(args.get("query", "")),
            memory_type=args.get("memory_type"),
            min_score=args.get("min_score", DEFAULT_MEMORY_MIN_SCORE),
            limit=args.get("limit", 8),
            access_context=access_context,
        )
    if action == "memory_history":
        return memory_history(space_id, str(args.get("query", "")), limit=args.get("limit", 10), access_context=access_context)
    if action == "list_tasks":
        return list_tasks(
            space_id,
            limit=args.get("limit", 30),
            status=args.get("status"),
            query=str(args.get("query") or ""),
            access_context=access_context,
        )
    if action == "list_recent_episodes":
        return list_recent_episodes(space_id, limit=args.get("limit", 10), access_context=access_context)
    if action == "profile_summary":
        return profile_summary(space_id, str(args.get("query", "")), limit=args.get("limit", 20), access_context=access_context)
    if action == "memory_note_fallback":
        return memory_note_fallback(
            space_id,
            str(args.get("query", "")),
            limit=args.get("limit", QUERY_TOP_K),
            min_score=args.get("min_score", DEFAULT_QUERY_MIN_SCORE),
        )

    return {"error": f"unknown tool: {action}"}


def _run_tool(
    space_id: str,
    action: str,
    args: dict[str, Any],
    *,
    trace: dict[str, Any] | None = None,
    hook_context: AgentRunContext | None = None,
) -> Any:
    """函数功能：`_run_tool` 负责运行 tool，服务于本文件职责：问答主编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        action: action 参数，由调用方传入，类型为 `str`。
        args: args 参数，由调用方传入，类型为 `dict[str, Any]`。
        trace: trace 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
        hook_context: hook context 参数，由调用方传入，类型为 `AgentRunContext | None`，默认值为 `None`。
    返回结果说明：
        返回 `Any` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    def execute() -> Any:
        """函数功能：`execute` 负责执行，服务于本文件职责：问答主编排。
        传参：
            无。
        返回结果说明：
            返回 `Any` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        with observe(
            "query.tool_call",
            space_id=space_id,
            extra=_safe_tool_args(action, args),
        ):
            return _execute_tool(space_id, action, args, access_context=(hook_context.metadata.get("access_context") if hook_context else None))

    result = get_default_hook_manager().run_tool(hook_context, action, args, execute) if hook_context else execute()
    step = "memory_search" if action == "memory_search" else "note_search"
    add_step(
        trace,
        step,
        input_summary=_safe_tool_args(action, args),
        output_summary={"result_count": len(_result_ids(result)), "ids": _result_ids(result)},
    )
    return result


def _history_fallback_answer(result: Any, *, question: str | None = None) -> str:
    items = [item for item in _evidence_items(result) if isinstance(item, dict)]
    if not items:
        return "我没有找到该记忆的历史版本。"
    seen: set[str] = set()
    ordered = sorted(items, key=lambda x: (int(x.get("version") or 0), str(x.get("created_at") or "")))
    statuses: list[str] = []
    topic = ""
    contents: list[str] = []
    for item in ordered:
        key = f"{item.get('memory_id') or item.get('id')}:{item.get('version')}"
        if key in seen:
            continue
        seen.add(key)
        status = str(item.get("task_status") or "").strip()
        content = str(item.get("content") or "").strip()
        if content:
            contents.append(content)
        if status and status not in statuses:
            statuses.append(status)
        if not topic and content:
            topic = content
    if statuses:
        for token in ("待处理", "被阻塞", "已完成", "当前状态为todo", "当前状态为blocked", "当前状态为done"):
            topic = topic.replace(token, "")
        topic = topic.strip(" ，,。；;：:") or "该记忆"
        if len(statuses) == 3 and statuses == ["todo", "blocked", "done"]:
            normalized_question = _normalized_query(question or "")
            if any(marker in normalized_question for marker in ("总结", "过程", "开始到完成")):
                return f"{topic}先处于todo，随后进入blocked，最后转为done。"
            return f"{topic}依次经历了todo、blocked、done。"
        return f"{topic}依次经历了{'、'.join(statuses)}。"
    preference_states: list[str] = []
    preference_topics: list[str] = []
    for content in contents:
        text = content.strip(" ，,。；;：:")
        if "不喜欢" in text:
            preference_states.append("negative")
            preference_topics.append(text.split("不喜欢", 1)[1].strip(" ，,。；;：:"))
        elif "减少" in text:
            preference_states.append("weakened")
            preference_topics.append(text.split("减少", 1)[1].strip(" ，,。；;：:"))
        elif "喜欢" in text:
            preference_states.append("positive")
            preference_topics.append(text.split("喜欢", 1)[1].strip(" ，,。；;：:"))
    preference_topics = [value for value in preference_topics if value]
    if preference_states and preference_topics:
        preference_topic = preference_topics[-1]
        if preference_states[0] == "positive" and preference_states[-1] == "negative":
            if "weakened" in preference_states[1:-1]:
                return f"你以前喜欢{preference_topic}，后来偏好减弱，现在不喜欢{preference_topic}。"
            return f"你以前喜欢{preference_topic}，现在不喜欢{preference_topic}。"
        if preference_states[0] == "negative" and preference_states[-1] == "positive":
            return f"你以前不喜欢{preference_topic}，现在喜欢{preference_topic}。"
    places: list[str] = []
    for content in contents:
        for marker in ("居住在", "居住于", "住在"):
            if marker in content:
                value = content.split(marker, 1)[1].strip(" ，,。；;：:")
                if value:
                    places.append(value)
                break
    places = list(dict.fromkeys(places))
    normalized_question = _normalized_query(question or "")
    if len(places) >= 2 and any(marker in normalized_question for marker in ("居住", "住在哪", "住哪里", "居住地")):
        return f"你以前住在{places[0]}，现在住在{places[-1]}。"
    content = str(ordered[-1].get("content") or ordered[0].get("content") or "").strip()
    if "用户曾居住在" in content:
        return content.replace("用户曾居住在", "你以前居住于").rstrip("。") + "。"
    return content.rstrip("。") + "。" if content else "我找到了历史记录，但缺少可直接展示的内容。"

def _inventory_fallback_answer(action: str, result: Any) -> str:
    items = result.get("slots", {}) if isinstance(result, dict) and action == "profile_summary" else result
    if action == "profile_summary":
        lines = ["用户画像摘要："]
        for memory_type, values in items.items():
            for item in values[:5]:
                lines.append(f"- [{memory_type}] {item.get('content') or item.get('summary') or item.get('object_value') or item.get('id')}")
        return chr(10).join(lines) if len(lines) > 1 else "暂无可用的画像记录。"
    values = [item for item in (items or []) if isinstance(item, dict)]
    if not values:
        return "暂无符合条件的记录。"
    # Keep one user-visible list item per evidence item.  A stand-alone title
    # is not a claim and otherwise weakens per-item answer/citation contracts.
    lines: list[str] = []
    for item in values[:10]:
        text = item.get("content") or item.get("object_value") or item.get("id")
        status = item.get("task_status") or item.get("status") or ""
        lines.append(f"- {text}" + (f"（{status}）" if status else ""))
    return chr(10).join(lines)


def _fallback_answer(observations: list[dict[str, Any]]) -> str:
    """函数功能：`_fallback_answer` 负责处理 fallback answer，服务于本文件职责：问答主编排。
    传参：
        observations: observations 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    candidates = []
    for observation in observations:
        result = observation.get("result")
        if isinstance(result, list):
            candidates.extend(item for item in result if isinstance(item, dict))
        elif isinstance(result, dict):
            if result.get("id"):
                candidates.append(result)
            for item in result.get("related", []):
                if isinstance(item, dict):
                    candidates.append(item)
            for group in result.get("related_groups", []):
                if not isinstance(group, dict):
                    continue
                source = group.get("source")
                if isinstance(source, dict):
                    candidates.append(source)
                for item in group.get("related", []):
                    if isinstance(item, dict):
                        candidates.append(item)

    if not candidates:
        return "我没有在随心记里找到足够相关的记录。"

    lines = ["我找到几条可能相关的记录："]
    for item in candidates[:3]:
        title = item.get("title") or item.get("id")
        summary = item.get("summary") or item.get("content") or item.get("text") or ""
        lines.append(f"- {title}：{summary}")
    return "\n".join(lines)


def _provisional_answer(notes: list[dict[str, Any]]) -> str:
    """函数功能：`_provisional_answer` 负责处理 provisional answer，服务于本文件职责：问答主编排。
    传参：
        notes: notes 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    lines = ["刚收到的记录还在后台完善分类，但已经可以查询："]
    for note in notes[:3]:
        content = note.get("text") or note.get("summary") or note.get("title") or ""
        lines.append(f"- {_clip(str(content), 220)}")
    return "\n".join(lines)


def _memory_still_updating_answer(notes: list[dict[str, Any]]) -> str:
    """函数功能：`_memory_still_updating_answer` 负责处理 memory still updating answer，服务于本文件职责：问答主编排。
    传参：
        notes: notes 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    prefix = "最新记录已保存，长期记忆仍在更新。"
    if not notes:
        return prefix + "请稍后再问一次。"
    return prefix + "\n\n" + _provisional_answer(notes)


def _complete_json_with_hooks(
    context: AgentRunContext | None,
    *,
    name: str,
    system_prompt: str,
    user_prompt: str,
    model_role: str = "balanced",
    llm_task: str | None = None,
) -> dict[str, Any]:
    """函数功能：`_complete_json_with_hooks` 负责完成 json with hooks，服务于本文件职责：问答主编排。
    传参：
        context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `AgentRunContext | None`。
        name: name 参数，由调用方传入，类型为 `str`。
        system_prompt: system prompt 参数，由调用方传入，类型为 `str`。
        user_prompt: user prompt 参数，由调用方传入，类型为 `str`。
        model_role: model role 参数，由调用方传入，类型为 `str`，默认值为 `'balanced'`。
        llm_task: llm task 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    def call() -> dict[str, Any]:
        """函数功能：`call` 负责调用，服务于本文件职责：问答主编排。
        传参：
            无。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
        try:
            return complete_json(system_prompt=system_prompt, user_prompt=user_prompt, model_role=model_role, llm_task=llm_task)
        except TypeError as exc:
            if "llm_task" not in str(exc):
                raise
            return complete_json(system_prompt=system_prompt, user_prompt=user_prompt, model_role=model_role)

    if context is None:
        return call()
    request: dict[str, Any] = {
        "name": name,
        "system_prompt_len": len(system_prompt),
        "user_prompt": user_prompt,
        "model_role": model_role,
    }
    return get_default_hook_manager().run_llm(
        context,
        request,
        call,
    )


def _synthesize_answer(question: str, observations: list[dict[str, Any]], *, hook_context: AgentRunContext | None = None) -> str:
    """函数功能：`_synthesize_answer` 负责处理 synthesize answer，服务于本文件职责：问答主编排。
    传参：
        question: 用户问题文本，类型为 `str`。
        observations: observations 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
        hook_context: hook context 参数，由调用方传入，类型为 `AgentRunContext | None`，默认值为 `None`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    payload = {
        "question": question,
        "observations": observations,
    }

    try:
        data = _complete_json_with_hooks(
            hook_context,
            name="query_synthesis",
            system_prompt=FINAL_SYSTEM_PROMPT,
            user_prompt=json.dumps(payload, ensure_ascii=False, indent=2),
            llm_task="query_synthesis",
        )
    except Exception:
        return _fallback_answer(observations)

    return str(data.get("final_answer") or "").strip() or _fallback_answer(observations)


def _answer_question_impl(space_id: str, question: str, max_steps: int, hook_context: AgentRunContext | None) -> str:
    """函数功能：`_answer_question_impl` 负责处理 answer question impl，服务于本文件职责：问答主编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        question: 用户问题文本，类型为 `str`。
        max_steps: max steps 参数，由调用方传入，类型为 `int`。
        hook_context: hook context 参数，由调用方传入，类型为 `AgentRunContext | None`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    question = question.strip()
    max_steps = max(1, min(int(max_steps), 4))
    trace = start_trace("memory_query", space_id, query_len=len(question))
    add_step(trace, "query_received", input_summary={"question_len": len(question), "max_steps": max_steps})
    with observe(
        "query.answer_question",
        space_id=space_id,
        extra={"question_len": len(question), "max_steps": max_steps},
    ):
        if not question:
            answer = "你想问什么？可以这样发：/ask 上次说的那件事是什么"
            _log_final_answer(space_id, answer, source="empty_question")
            add_step(trace, "answer_returned", output_summary={"answer_len": len(answer)}, reason="empty_question")
            finish_trace(trace)
            _store_answer_evidence(hook_context, answer_source="empty_question", selected_evidence=[], observations=[])
            return answer

        if mentions_sensitive_topic(question):
            answer = "为保护安全，随心记不会保存或检索密码、密钥、令牌、身份证号、银行卡号等敏感凭据。"
            _log_final_answer(space_id, answer, source="sensitive_query_blocked")
            add_step(
                trace,
                "query_blocked",
                status="discarded",
                output_summary={"reason": "sensitive_topic"},
                reason="sensitive_topic",
            )
            add_step(trace, "answer_returned", output_summary={"answer_len": len(answer)})
            finish_trace(trace)
            _store_answer_evidence(hook_context, answer_source="sensitive_query_blocked", selected_evidence=[], observations=[])
            return answer

        observations: list[dict[str, Any]] = []
        selected_evidence: list[dict[str, Any]] = []
        query_plan = build_query_plan(question)
        add_step(
            trace,
            "query_plan",
            output_summary={
                "complexity": query_plan.complexity,
                "use_query_rewrite": query_plan.use_query_rewrite,
                "use_decomposition": query_plan.use_decomposition,
                "use_step_back": query_plan.use_step_back,
                "variant_count": len(query_plan.retrieval_queries),
                "routing_state": query_plan.routing_state,
                "routing_confidence": query_plan.routing_confidence,
                "routing_reasons": list(query_plan.routing_reasons),
            },
            reason="conditional_complex_query_features",
        )
        if hook_context is not None and hook_context.session:
            session_context = {
                key: hook_context.session.get(key)
                for key in ("current_intent", "waiting_for", "pending_operation", "conversation_summary")
                if hook_context.session.get(key) is not None
            }
            if session_context:
                observations.append({"thought": "恢复上一轮临时会话。", "tool": "session_context", "args": {}, "result": session_context})

        try:
            provisional = provisional_search(space_id, question, limit=5)
            fast_route = _deterministic_route(question) or _intent_route(question, trace=trace)
            model_plan = fast_route.get("query_plan") if isinstance(fast_route, dict) else None
            if isinstance(model_plan, dict):
                query_plan = build_query_plan(question, model_plan=model_plan)
                add_step(
                    trace,
                    "query_plan_model",
                    output_summary={
                        "complexity": query_plan.complexity,
                        "routing_state": query_plan.routing_state,
                        "variant_count": len(query_plan.retrieval_queries),
                        "strategies": {
                            "rewrite": query_plan.use_query_rewrite,
                            "decomposition": query_plan.use_decomposition,
                            "step_back": query_plan.use_step_back,
                        },
                    },
                    reason="validated_fast_llm_plan",
                )
            if fast_route is not None and str(fast_route.get("action")) == "memory_prefetch":
                # 结构化意图已判断它不属于有界 Memory 优先快路径，因此继续走常规 memory-prefetch/ReAct 流程。
                fast_route = None
            if (
                fast_route is not None
                and str(fast_route.get("action")) == "memory_search"
                and query_plan.complexity == "complex"
                and query_plan.routing_state == "complex"
            ):
                # 复杂状态问题需要有界多查询召回；只按类型走快路径可能提前返回一个主题，遮住对比问题的另一半。
                fast_route = None
            if provisional:
                observations.append(
                    {
                        "thought": "新笔记已本地落库，后台增强尚未结束。",
                        "tool": "provisional_search",
                        "args": {"query_len": len(question), "limit": 5},
                        "result": provisional,
                    }
                )
                add_step(
                    trace,
                    "query_routed",
                    output_summary={"tool": "provisional_search", "safe_args": {"query_len": len(question), "limit": 5}},
                    reason="read_after_write",
                )
                add_step(trace, "note_search", output_summary={"result_count": len(provisional), "ids": _result_ids(provisional)})
                # 立即回答路径刻意不依赖查询意图和向量可用性；这里的词法命中就是刚持久化的 Note。
                # 若等待 Memory worker 或调用模型，用户追问时可能误以为刚才的消息丢失。
                add_step(trace, "evidence_selected", output_summary={"ids": _result_ids(provisional)})
                add_step(trace, "rerank", output_summary={"strategy": "local_lexical_recency", "ids": _result_ids(provisional)})
                answer = _with_sources(_provisional_answer(provisional), provisional)
                _log_final_answer(space_id, answer, source="provisional_read_after_write", observations=observations)
                add_step(trace, "answer_generated", output_summary={"answer_len": len(answer)}, reason="no_llm_wait")
                add_step(trace, "answer_returned", output_summary={"answer_len": len(answer)})
                finish_trace(trace)
                _store_answer_evidence(hook_context, answer_source="provisional_read_after_write", selected_evidence=provisional, observations=observations)
                return answer

            if fast_route is not None:
                action = str(fast_route["action"])
                args = dict(fast_route["args"])
                barrier: dict[str, Any] | None = None
                if action == "memory_search":
                    barrier = wait_for_memory_barrier(space_id)
                    add_step(
                        trace,
                        "memory_watermark_barrier",
                        status="partial" if barrier.get("status") == "timeout" else "success",
                        output_summary=barrier,
                        reason="memory_first_read_after_write",
                    )
                add_step(
                    trace,
                    "query_routed",
                    output_summary={"tool": action, "safe_args": _safe_tool_args(action, args)},
                    reason=f"fast_path:{fast_route['reason']}",
                )
                result = _run_tool(space_id, action, args, trace=trace, hook_context=hook_context)
                observations.append(
                    {
                        "thought": f"确定性快速路由：{fast_route['reason']}",
                        "tool": action,
                        "args": _safe_tool_args(action, args),
                        "result": result,
                    }
                )
                fallback = fast_route.get("fallback")
                fallback_used = False
                if not result and isinstance(fallback, dict):
                    fallback_action = str(fallback["action"])
                    fallback_args = dict(fallback["args"])
                    fallback_result = _run_tool(
                        space_id,
                        fallback_action,
                        fallback_args,
                        trace=trace,
                        hook_context=hook_context,
                    )
                    observations.append(
                        {
                            "thought": "快速路径主存储无结果，使用受限降级查询。",
                            "tool": fallback_action,
                            "args": _safe_tool_args(fallback_action, fallback_args),
                            "result": fallback_result,
                        }
                    )
                    result = fallback_result
                    fallback_used = True
                if not result and barrier is not None and barrier.get("status") == "timeout":
                    answer = _with_sources(_memory_still_updating_answer(provisional), selected_evidence)
                    _log_final_answer(space_id, answer, source="memory_barrier_timeout", observations=observations)
                    add_step(trace, "answer_generated", output_summary={"answer_len": len(answer)}, reason="memory_barrier_timeout")
                    add_step(trace, "answer_returned", output_summary={"answer_len": len(answer)})
                    finish_trace(trace)
                    _store_answer_evidence(hook_context, answer_source="memory_barrier_timeout", selected_evidence=selected_evidence, observations=observations)
                    return answer
                if not result and provisional:
                    result = provisional
                if result:
                    selected_evidence = _merge_evidence(selected_evidence, result)
                add_step(trace, "evidence_selected", output_summary={"ids": _result_ids(result)})
                add_step(trace, "rerank", output_summary={"strategy": "fast_path_tool_order", "ids": _result_ids(result)})
                if action == "memory_history" and result:
                    answer = _history_fallback_answer(result, question=question)
                    reason = "history_deterministic_timeline"
                elif action in {"list_tasks", "list_recent_episodes", "profile_summary"} and result and not fallback_used:
                    answer = _inventory_fallback_answer(action, result)
                    reason = "structured_inventory_template"
                elif fast_route["synthesize"] and result:
                    answer = _synthesize_answer(question, observations, hook_context=hook_context)
                    reason = "fast_path_single_synthesis"
                else:
                    answer = _fallback_answer(observations)
                    reason = "fast_path_deterministic_answer"
                answer = _with_sources(answer, selected_evidence)
                _log_final_answer(space_id, answer, source=reason, observations=observations)
                add_step(trace, "answer_generated", output_summary={"answer_len": len(answer)}, reason=reason)
                add_step(trace, "answer_returned", output_summary={"answer_len": len(answer)})
                finish_trace(trace)
                _store_answer_evidence(hook_context, answer_source=reason, selected_evidence=selected_evidence, observations=observations)
                return answer

            add_step(
                trace,
                "query_routed",
                output_summary={
                    "tool": "memory_search",
                    "safe_args": {"tool": "memory_search", "query_len": len(question), "limit": 5, "min_score": DEFAULT_MEMORY_MIN_SCORE},
                },
                reason="prefetch_active_memory",
            )
            #先做一次简单查询
            memory_prefetch = _run_tool(
                space_id,
                "memory_search",
                {"query": question, "limit": 5, "min_score": DEFAULT_MEMORY_MIN_SCORE},
                trace=trace,
                hook_context=hook_context,
            )
            # 复杂查询变体也用于弱但非空的召回和多主题拆解；这样能补救看似合理但不完整的候选，同时不影响简单查询。
            # 确实是复杂查询，已经生成检索变体，元查询结果较弱时，进行扩展查询
            should_expand = (
                query_plan.complexity == "complex"
                and bool(query_plan.retrieval_queries)
                and (
                    _low_quality_memory_result(memory_prefetch)
                    or query_plan.use_decomposition
                    or query_plan.use_step_back
                )
            )
            executed_variant_count = 0
            skipped_variant_count = 0
            if should_expand:
                result_groups: list[list[dict[str, Any]]] = [memory_prefetch] if isinstance(memory_prefetch, list) else []
                for variant in query_plan.retrieval_queries:
                    if variant == question:
                        skipped_variant_count += 1
                        continue
                    is_step_back = variant.endswith((
                        "背景与原因", "历史变化与趋势", "共同点、差异与适用场景",
                        "方法、步骤与注意事项", "上位概念、背景与约束",
                    ))
                    is_rewrite = variant == query_plan.rewritten_query
                    if is_rewrite and not settings.QUERY_REWRITE_ENABLED:
                        skipped_variant_count += 1
                        continue
                    if is_step_back and not settings.QUERY_STEP_BACK_ENABLED:
                        skipped_variant_count += 1
                        continue
                    if not is_rewrite and not is_step_back and not settings.QUERY_DECOMPOSITION_ENABLED:
                        skipped_variant_count += 1
                        continue
                    executed_variant_count += 1
                    variant_result = _run_tool(
                        space_id,
                        "memory_search",
                        {"query": variant, "limit": 5, "min_score": DEFAULT_MEMORY_MIN_SCORE},
                        trace=trace,
                        hook_context=hook_context,
                    )
                    add_step(
                        trace,
                        "query_variant",
                        output_summary={"query_len": len(variant), "result_count": len(variant_result or [])},
                        reason="query_rewrite_or_complex_recall",
                    )
                    if isinstance(variant_result, list) and variant_result:
                        result_groups.append(variant_result)
                        observations.append(
                            {
                                "thought": "复杂查询使用受限检索变体补充召回。",
                                "tool": "memory_search",
                                "args": {"query_len": len(variant), "limit": 5, "min_score": DEFAULT_MEMORY_MIN_SCORE},
                                "result": variant_result,
                            }
                        )
                #原始排名高、多个子问题都命中、自身检索分数高
                if result_groups:
                    memory_prefetch = _fuse_memory_results(result_groups, limit=5)
            add_step(
                trace,
                "query_plan_executed",
                output_summary={
                    "planned_variant_count": len(query_plan.retrieval_queries),
                    "executed_variant_count": executed_variant_count,
                    "skipped_variant_count": skipped_variant_count,
                    "expansion_gate": should_expand,
                },
                reason="bounded_variant_execution",
            )
            if memory_prefetch:
                selected_evidence = _merge_evidence(selected_evidence, memory_prefetch)
                observations.append(
                    {
                        "thought": "先召回最新 active 长期记忆。",
                        "tool": "memory_search",
                        "args": {"query_len": len(question), "limit": 5, "min_score": DEFAULT_MEMORY_MIN_SCORE},
                        "result": memory_prefetch,
                    }
                )
                add_step(trace, "evidence_selected", output_summary={"ids": _result_ids(memory_prefetch)})

            # 多子句问题即使命中某个 Memory，也可能还需要原始 Note 语境；该补充限定在计划变体内，保证普通查询仍以 Memory 优先。
            if (
                query_plan.complexity == "complex"
                and query_plan.use_decomposition
                and settings.COMPLEX_QUERY_NOTE_AUGMENT_ENABLED
            ):
                note_limit = max(1, min(int(settings.COMPLEX_QUERY_NOTE_AUGMENT_LIMIT), QUERY_TOP_K))
                for variant in dict.fromkeys(query_plan.retrieval_queries):
                    note_result = _run_tool(
                        space_id,
                        "memory_note_fallback",
                        {"query": variant, "limit": note_limit, "min_score": DEFAULT_QUERY_MIN_SCORE},
                        trace=trace,
                        hook_context=hook_context,
                    )
                    add_step(
                        trace,
                        "query_variant_note",
                        output_summary={"query_len": len(variant), "result_count": len(note_result or [])},
                        reason="complex_clause_note_augmentation",
                    )
                    if isinstance(note_result, list) and note_result:
                        selected_evidence = _merge_evidence(selected_evidence, note_result)
                        observations.append(
                            {
                                "thought": "复杂查询按子句补充原始笔记证据。",
                                "tool": "memory_note_fallback",
                                "args": {"query_len": len(variant), "limit": note_limit, "min_score": DEFAULT_QUERY_MIN_SCORE},
                                "result": note_result,
                            }
                        )
                add_step(
                    trace,
                    "evidence_selected",
                    output_summary={"ids": _result_ids(selected_evidence)},
                    reason="memory_and_note_clause_coverage",
                )

            react_llm_task = "query_complex_reasoning" if query_plan.complexity == "complex" or max_steps > 2 else "query_routing"
            #ReAct主体
            for step in range(max_steps):
                payload = {
                    "question": question,
                    "step": step + 1,
                    "observations": observations,
                }

                try:
                    decision = _complete_json_with_hooks(
                        hook_context,
                        name="query_react",
                        system_prompt=REACT_SYSTEM_PROMPT,
                        user_prompt=json.dumps(payload, ensure_ascii=False, indent=2),
                        model_role="fast" if react_llm_task == "query_routing" else "strong",
                        llm_task=react_llm_task,
                    )
                except Exception as exc:
                    if not observations:
                        raise
                    answer = _with_sources(_fallback_answer(observations), selected_evidence)
                    _log_final_answer(space_id, answer, source="react_fallback_after_error", observations=observations)
                    add_step(trace, "answer_generated", output_summary={"answer_len": len(answer)}, reason="react_fallback_after_error")
                    add_step(trace, "answer_returned", output_summary={"answer_len": len(answer)})
                    finish_trace(trace)
                    log_event(
                        "query.react_fallback",
                        level="warning",
                        status="success",
                        space_id=space_id,
                        error=f"{type(exc).__name__}: {exc}",
                        extra={"observation_count": len(observations)},
                    )
                    _store_answer_evidence(hook_context, answer_source="react_fallback_after_error", selected_evidence=selected_evidence, observations=observations)
                    return answer

                final_answer = str(decision.get("final_answer") or "").strip()
                if hook_context is not None and "session_update" in decision:
                    update = decision.get("session_update")
                    hook_context.metadata["session_update"] = update if isinstance(update, dict) else {}
                if final_answer and observations:
                    cited_evidence = _cited_evidence(observations, decision.get("evidence_ids"))
                    source_evidence = cited_evidence or selected_evidence
                    add_step(
                        trace,
                        "answer_evidence_cited",
                        output_summary={"ids": _result_ids(source_evidence)},
                        reason="llm_citations" if cited_evidence else "selected_evidence_fallback",
                    )
                    answer = _with_sources(final_answer, source_evidence)
                    _log_final_answer(space_id, answer, source="react_final", observations=observations)
                    add_step(trace, "answer_generated", output_summary={"answer_len": len(final_answer)}, reason="react_final")
                    add_step(trace, "answer_returned", output_summary={"answer_len": len(answer)})
                    finish_trace(trace)
                    _store_answer_evidence(hook_context, answer_source="react_final", selected_evidence=source_evidence, observations=observations)
                    return answer

                action = decision.get("action")
                args = decision.get("args") or {}

                if not action:
                    action = "semantic_search"
                    args = {
                        "query": question,
                        "top_k": QUERY_TOP_K,
                        "min_score": DEFAULT_QUERY_MIN_SCORE,
                    }

                if not isinstance(args, dict):
                    args = {}

                add_step(
                    trace,
                    "query_routed",
                    input_summary={"step": step + 1},
                    output_summary={"tool": action, "safe_args": _safe_tool_args(str(action), args)},
                    reason=str(decision.get("thought") or ""),
                )

                result = _run_tool(space_id, str(action), args, trace=trace, hook_context=hook_context)
                observations.append(
                    {
                        "thought": decision.get("thought"),
                        "tool": action,
                        "args": args,
                        "result": result,
                    }
                )
                if result:
                    selected_evidence = _merge_evidence(selected_evidence, result)
                add_step(trace, "evidence_selected", output_summary={"ids": _result_ids(result)})
                add_step(trace, "rerank", output_summary={"strategy": "tool_order", "ids": _result_ids(result)})

            answer = _with_sources(_synthesize_answer(question, observations, hook_context=hook_context), selected_evidence)
            _log_final_answer(space_id, answer, source="synthesized", observations=observations)
            add_step(trace, "answer_generated", output_summary={"answer_len": len(answer)}, reason="synthesized")
            add_step(trace, "answer_returned", output_summary={"answer_len": len(answer)})
            finish_trace(trace)
            _store_answer_evidence(hook_context, answer_source="synthesized", selected_evidence=selected_evidence, observations=observations)
            return answer
        except Exception as exc:
            add_step(trace, "answer_failed", status="failed", error=str(exc))
            finish_trace(trace, status="failed")
            raise


def _run_answer_question_with_context(
    space_id: str,
    question: str,
    max_steps: int,
    *,
    tenant_id: str = "default",
    user_id: str | None = None,
    message_id: str | None = None,
    task_id: str | None = None,
    access_context: dict[str, Any] | AccessContext | None = None,
) -> tuple[str, AgentRunContext]:
    context = AgentRunContext.create(
        space_id=space_id,
        run_type="query",
        tenant_id=tenant_id,
        user_id=user_id,
        message_id=message_id,
        task_id=task_id,
        metadata={"question_len": len(question), "max_steps": max_steps, "access_context": _memory_access_context(access_context, requester=user_id or "owner")},
    )
    answer = get_default_hook_manager().run_agent(
        context,
        lambda: _answer_question_impl(space_id, question, max_steps, context),
    )
    return answer, context


def answer_question(
    space_id: str,
    question: str,
    max_steps: int = 4,
    *,
    tenant_id: str = "default",
    user_id: str | None = None,
    message_id: str | None = None,
    task_id: str | None = None,
    access_context: dict[str, Any] | AccessContext | None = None,
) -> str:
    """函数功能：`answer_question` 负责处理 answer question，服务于本文件职责：问答主编排。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        question: 用户问题文本，类型为 `str`。
        max_steps: max steps 参数，由调用方传入，类型为 `int`，默认值为 `4`。
        tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str`，默认值为 `'default'`。
        user_id: 用户标识，用于鉴权、限流、会话和数据归属，类型为 `str | None`，默认值为 `None`。
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str | None`，默认值为 `None`。
        task_id: 任务标识，用于查询、更新或幂等处理任务状态，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    answer, _ = _run_answer_question_with_context(
        space_id,
        question,
        max_steps,
        tenant_id=tenant_id,
        user_id=user_id,
        message_id=message_id,
        task_id=task_id,
        access_context=access_context,
    )
    return answer


def _answer_is_no_answer(answer: str) -> bool:
    text = str(answer or "")
    return any(token in text for token in ("没有在随心记里找到", "没有找到", "没有足够信息", "无法确认", "没有相关记录", "暂无足够", "还没有关于", "没有关于", "我不知道", "无法确定"))


def answer_question_result(
    space_id: str,
    question: str,
    max_steps: int = 4,
    *,
    tenant_id: str = "default",
    user_id: str | None = None,
    message_id: str | None = None,
    task_id: str | None = None,
    access_context: dict[str, Any] | AccessContext | None = None,
) -> "AnswerResult":
    """Structured contract; the legacy answer_question API remains string-returning."""
    from agent.answer_models import AnswerResult, AnswerDecision, EvidenceBundle, RetrievalEvidence
    context = _memory_access_context(access_context, requester=user_id or "owner")
    try:
        if mentions_sensitive_topic(question):
            answer = "为保护安全，随心记不会保存或检索密码、密钥、令牌、身份证号、银行卡号等敏感凭据。"
            return AnswerResult("restricted", answer, "sensitive_topic", decision=AnswerDecision("restricted", "sensitive_topic"))
        route = _deterministic_route(question)
        evidence_limit = max(5, min(max_steps * 2, 20))
        fetched_evidence = _memory_search_compat(space_id, question, min_score=0.0, limit=evidence_limit, access_context=context)
        rewrites = _mixed_language_query_rewrites(question)
        rewritten_results = [
            _memory_search_compat(space_id, rewrite, min_score=0.0, limit=evidence_limit, access_context=context)
            for rewrite in rewrites
        ]
        if rewritten_results:
            fetched_evidence = _fuse_memory_results([fetched_evidence, *rewritten_results], limit=evidence_limit)
        evidence_question = rewrites[0] if rewrites else question
        evidence = _relevant_evidence_items(evidence_question, fetched_evidence)
        history_evidence = memory_history(space_id, question, limit=evidence_limit, access_context=context) if route and route.get("action") == "memory_history" else []
        if route and route.get("action") == "memory_history" and history_evidence:
            evidence = []
        stale_history = [] if history_evidence else _stale_history_fallback(space_id, question, limit=evidence_limit, access_context=context)
        if not evidence and stale_history:
            history_evidence = stale_history
        pending_review_ids: list[str] = []
        if _query_conflict_intent(question):
            pending = [
                memory.to_dict()
                for memory in list_memories(space_id, status="pending_review", limit=evidence_limit)
            ]
            if context is not None:
                from memory.access import memory_access_allowed
                pending = [item for item in pending if memory_access_allowed(item, context)]
            pending = _relevant_evidence_items(question, pending, floor=0.0)
            evidence = _merge_evidence(evidence, pending)
            evidence_ids = {str(item.get("id") or item.get("memory_id") or "") for item in evidence}
            for review in list_memory_decisions(space_id, status="pending_review", limit=evidence_limit):
                review_refs = {
                    str(ref)
                    for ref in [
                        *(review.get("target_memory_ids") or []),
                        *(review.get("result_memory_ids") or []),
                    ]
                    if ref
                }
                if review_refs & evidence_ids:
                    pending_review_ids.append(str(review.get("id") or ""))
        all_evidence = list(evidence) + list(history_evidence)

        structured_bundle = _build_evidence_bundle(all_evidence, [])
        for item in structured_bundle.items:
            if item.role == "current" and item.status in {"pending_review", "pending", "conflicted"}:
                item.role = "conflict"
            if item.role == "current" and item.status in {"superseded", "expired", "archived", "forgotten", "deleted"}:
                item.role = "stale_history"
            item.selected = True
        structured_bundle.__post_init__()
        restricted_denied = False
        if not fetched_evidence and context and context.get("requester") not in {None, "owner"}:
            unrestricted = _memory_search_compat(
                space_id,
                question,
                min_score=0.0,
                limit=3,
                access_context={"requester": "owner", "owner_id": "owner", "allow_sensitive": True, "allow_restricted": True},
            )
            if unrestricted:
                restricted_denied = True
                structured_bundle.items.append(
                    RetrievalEvidence(
                        kind="access_denied",
                        id="access_denied",
                        selected=True,
                        role="access_denied",
                        metadata={"reason": "acl_filtered_all_evidence"},
                    )
                )
                structured_bundle.__post_init__()
        answer, run_context = _run_answer_question_with_context(
            space_id,
            question,
            max_steps,
            tenant_id=tenant_id,
            user_id=user_id,
            message_id=message_id,
            task_id=task_id,
            access_context=context,
        )
        runtime_bundle = EvidenceBundle.from_dict(run_context.metadata.get("answer_evidence_bundle")) if isinstance(run_context.metadata.get("answer_evidence_bundle"), dict) else None
        if runtime_bundle is not None:
            runtime_selected = [
                {"id": item.memory_id or item.version_id or item.id, "memory_id": item.memory_id, "memory_type": item.memory_type, "status": item.status, "task_status": item.task_status, "score": item.score, "content": item.metadata.get("content")}
                for item in runtime_bundle.items
                if item.selected
            ]
            if _query_requested_attribute(evidence_question):
                runtime_selected = _relevant_evidence_items(evidence_question, runtime_selected)
            if runtime_selected and not evidence and not history_evidence and not _answer_is_no_answer(answer):
                evidence = runtime_selected
            # The runtime path is authoritative: it contains the exact
            # deterministic profile/list tool output selected for the answer.
            # The prefetch bundle is only a fallback when the runtime path did
            # not expose evidence at all.
            answer_bundle = runtime_bundle if runtime_bundle.items else structured_bundle
            if answer_bundle is not None:
                if not answer_bundle.selected_tool_refs:
                    answer_bundle.selected_tool_refs = list(runtime_bundle.selected_tool_refs)
                if not answer_bundle.executed_tools:
                    answer_bundle.executed_tools = list(runtime_bundle.executed_tools)
        else:
            answer_bundle = structured_bundle
        decision = decide_answer(
            question,
            route,
            answer_bundle,
            current_evidence=evidence,
            history_evidence=history_evidence,
            restricted_denied=restricted_denied,
            pending_review_ids=pending_review_ids,
        )

        if decision.answer_type == "restricted":
            answer = "这条记录受访问权限保护，当前请求无权查看具体内容。"
        elif decision.answer_type == "conflict":
            answer = _conflict_answer(evidence)
        elif decision.answer_type == "clarification":
            answer = _clarification_answer(evidence)
        elif decision.answer_type == "qualified_history_only":
            answer = "我没有找到当前有效记录；只找到历史记录：\n" + _history_fallback_answer(history_evidence, question=question)
        elif decision.answer_type == "answered" and decision.reason_code == "history_query" and history_evidence:
            answer = _history_fallback_answer(history_evidence, question=question)
        elif decision.answer_type == "answered" and (not answer or _answer_is_no_answer(answer)) and evidence:
            answer = _selected_evidence_answer(evidence[0])
        elif decision.answer_type == "answered" and evidence and str(evidence[0].get("memory_type") or "") == "semantic" and _query_current_intent(question):
            content = str(evidence[0].get("content") or "")
            if content and content not in answer:
                answer = _selected_evidence_answer(evidence[0])
        elif decision.answer_type == "no_answer":
            answer = "我没有在随心记里找到足够相关的记录。"
            answer_bundle = EvidenceBundle(items=[])
        if answer_bundle is not None and decision.answer_type in {"answered", "qualified_history_only", "conflict"}:
            source_basis = history_evidence if decision.reason_code in {"history_query", "stale_history_only"} else evidence
            answer = _with_sources(answer.split("来源（", 1)[0].rstrip(), source_basis)

        citations = re.findall(r"memory:([^｜\s]+)", answer or "")
        selected_memory_ids = list(answer_bundle.selected_memory_ids) if answer_bundle is not None else []
        selected_versions = list(answer_bundle.selected_version_ids) if answer_bundle is not None else []
        selected_source_ids = list(answer_bundle.selected_source_ids) if answer_bundle is not None else []
        selected_context_refs = list(answer_bundle.selected_context_refs) if answer_bundle is not None else []
        selected_tool_refs = list(answer_bundle.selected_tool_refs) if answer_bundle is not None else []
        executed_tools = list(answer_bundle.executed_tools) if answer_bundle is not None else []
        evidence_bundle = answer_bundle
        answer_type, reason = decision.answer_type, decision.reason_code
        claims = _supported_claims_from_bundle(answer_bundle, answer_type=answer_type, reason_code=reason)
        claim_groups = _timeline_claim_groups(
            answer_bundle,
            claims,
            answer_type=answer_type,
            reason_code=reason,
            answer=answer,
        )
        return AnswerResult(
            answer_type,
            answer,
            reason,
            claims=claims,
            claim_groups=claim_groups,
            citations=[{"memory_id": item} for item in citations],
            selected_memory_ids=selected_memory_ids,
            selected_version_ids=selected_versions,
            selected_source_ids=selected_source_ids,
            selected_context_refs=selected_context_refs,
            selected_tool_refs=selected_tool_refs,
            executed_tools=executed_tools,
            evidence_bundle=evidence_bundle,
            evidence_mode="history" if reason in {"history_query", "stale_history_only"} else "current",
            decision=decision,
        )
    except Exception as exc:
        return AnswerResult("system_error", "", f"{type(exc).__name__}:{exc}", retryable=True, decision=AnswerDecision("system_error", "query_exception"))
