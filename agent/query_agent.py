"""Query ReAct agent for searching and reasoning over stored notes."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from agent.hooks import AgentRunContext, get_default_hook_manager
from core.llm_client import complete_json, embed_text
from core.observability import log_event, observe
from core.sensitive import mentions_sensitive_topic
from core.settings import MEMORY_QUERY_MIN_SCORE, QUERY_MIN_SCORE, QUERY_TOP_K, STORAGE_BACKEND
from core.taxonomy import is_valid_tag, is_valid_type, normalize_tag, normalize_type
from memory.service import memory_search
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
{{"thought":"为什么可以回答","final_answer":"基于笔记的回答"}}

规则：
- 如果用户明确说“type 是生活/学习/任务”等，调用 filter_notes，不要调用 semantic_search。
- 如果用户明确说“标签是饮食/提醒/问题”等，调用 filter_notes，不要调用 semantic_search。
- 如果用户同时给出 type 和 tags，调用 filter_notes。
- 用户没有明确 type/tags，或只是用自然语言描述想找的内容时，才调用 semantic_search。
- 用户通常不知道 note_id。调用 follow_links 前，必须先通过 semantic_search、filter_notes 或 list_recent 找到候选 note_id。
- 如果用户问长期偏好、习惯、当前任务状态或“我现在/我喜欢/我住在哪/我重点做什么”，优先调用 memory_search。
- 如果用户问“和某条笔记相关的有哪些”，先 semantic_search 找候选 note_id，再 follow_links。
- 回答只能基于 observations，不要编造。
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
_CURRENT_TASK_MARKERS = ("当前待办", "现在的任务", "有哪些任务", "要做什么", "任务进度", "待办是什么")
_CURRENT_FACT_MARKERS = ("住在哪里", "住哪", "现在住", "目前住", "正在学习", "重点做什么", "当前项目")


def _clip(text: str | None, limit: int = 500) -> str:
    if not text:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _parse_ts(ts: str | None) -> datetime | None:
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
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _coerce_bool(value: Any, default: bool = True) -> bool:
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
    return " ".join(value.strip().casefold().rstrip("?？").split())


def _deterministic_route(question: str) -> dict[str, Any] | None:
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
                "action": "semantic_search",
                "args": {"query": normalized, "top_k": QUERY_TOP_K, "min_score": DEFAULT_QUERY_MIN_SCORE},
            },
            "synthesize": True,
            "reason": "current_preference",
        }
    if any(marker in normalized for marker in _CURRENT_TASK_MARKERS):
        return {
            "action": "memory_search",
            "args": {"query": normalized, "memory_type": "task", "limit": 8, "min_score": DEFAULT_MEMORY_MIN_SCORE},
            "fallback": {"action": "filter_notes", "args": {"type": "任务", "limit": 8}},
            "synthesize": True,
            "reason": "current_task",
        }
    if any(marker in normalized for marker in _CURRENT_FACT_MARKERS):
        return {
            "action": "memory_search",
            "args": {"query": normalized, "memory_type": "semantic", "limit": 5, "min_score": DEFAULT_MEMORY_MIN_SCORE},
            "fallback": {
                "action": "semantic_search",
                "args": {"query": normalized, "top_k": QUERY_TOP_K, "min_score": DEFAULT_QUERY_MIN_SCORE},
            },
            "synthesize": True,
            "reason": "current_fact",
        }
    if len(normalized) <= 60 and not any(marker in normalized for marker in _COMPLEX_QUERY_MARKERS):
        return {
            "action": "semantic_search",
            "args": {"query": normalized, "top_k": QUERY_TOP_K, "min_score": DEFAULT_QUERY_MIN_SCORE},
            "synthesize": True,
            "reason": "single_hop_semantic",
        }
    return None


def _safe_tool_args(action: str, args: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {"tool": action}
    for key in ("type", "note_type", "tags", "tag", "limit", "top_k", "min_score", "days", "note_id", "match_all_tags", "memory_type"):
        if key in args:
            safe[key] = args.get(key)
    if "query" in args:
        safe["query_len"] = len(str(args.get("query") or ""))
    return safe


def _result_ids(result: Any) -> list[str]:
    ids: list[str] = []
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
    elif isinstance(result, dict):
        if result.get("id"):
            ids.append(str(result["id"]))
        for key in ("related", "candidates"):
            for item in result.get(key, []) if isinstance(result.get(key), list) else []:
                if isinstance(item, dict) and item.get("id"):
                    ids.append(str(item["id"]))
    return ids[:10]


def _source_lines(observations: list[dict[str, Any]], limit: int = 5) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for observation in observations:
        result = observation.get("result")
        items: list[dict[str, Any]] = []
        if isinstance(result, list):
            items.extend(item for item in result if isinstance(item, dict))
        elif isinstance(result, dict):
            items.append(result)
            items.extend(item for item in result.get("related", []) if isinstance(item, dict))
            items.extend(item for item in result.get("candidates", []) if isinstance(item, dict))

        for item in items:
            item_id = str(item.get("id") or "")
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            if item.get("memory_type"):
                source_count = len(item.get("sources") or [])
                lines.append(f"- memory:{item_id}｜{item.get('memory_type')}｜sources={source_count}")
            else:
                title = item.get("title") or item_id
                time = item.get("time") or item.get("ts") or ""
                lines.append(f"- note:{item_id}｜{title}｜{str(time)[:10]}")
            if len(lines) >= limit:
                return lines
    return lines


def _with_sources(answer: str, observations: list[dict[str, Any]]) -> str:
    sources = _source_lines(observations)
    if not sources:
        return answer
    return answer.rstrip() + "\n\n来源：\n" + "\n".join(sources)


def _log_final_answer(
    space_id: str,
    answer: str,
    *,
    source: str,
    observations: list[dict[str, Any]] | None = None,
) -> None:
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
    return [note for note in load_index(space_id) if is_note_queryable(note)]


def _find_note(space_id: str, note_id: str) -> dict[str, Any] | None:
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
    """Filter notes deterministically by fixed type and fixed tags."""
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
    return filter_notes(
        space_id,
        note_type=note_type,
        limit=limit,
    )


def by_tag(space_id: str, tag: str, limit: int = 10) -> list[dict[str, Any]]:
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
    query = query.strip()
    if not query:
        return []

    top_k = max(1, min(int(top_k), 10))
    min_score = float(min_score)
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
    """Search newly saved notes locally while LLM enrichment is still running."""
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


def get_note(space_id: str, note_id: str) -> dict[str, Any]:
    note = _postgres_find_note(space_id, note_id) if STORAGE_BACKEND == "postgres" else _find_note(space_id, note_id)
    if note is not None and not is_note_queryable(note):
        note = None
    if note is None:
        return {"error": f"note not found: {note_id}"}
    return _note_brief(note, text_limit=1200)


def list_recent(space_id: str, days: int = 7, limit: int = 10) -> list[dict[str, Any]]:
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
    """Find a note by natural language and return its bidirectional related notes."""
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


def _execute_tool(space_id: str, action: str, args: dict[str, Any]) -> Any:
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
        return memory_search(
            space_id,
            str(args.get("query", "")),
            memory_type=args.get("memory_type"),
            min_score=args.get("min_score", DEFAULT_MEMORY_MIN_SCORE),
            limit=args.get("limit", 8),
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
    def execute() -> Any:
        with observe(
            "query.tool_call",
            space_id=space_id,
            extra=_safe_tool_args(action, args),
        ):
            return _execute_tool(space_id, action, args)

    result = get_default_hook_manager().run_tool(hook_context, action, args, execute) if hook_context else execute()
    step = "memory_search" if action == "memory_search" else "note_search"
    add_step(
        trace,
        step,
        input_summary=_safe_tool_args(action, args),
        output_summary={"result_count": len(_result_ids(result)), "ids": _result_ids(result)},
    )
    return result


def _fallback_answer(observations: list[dict[str, Any]]) -> str:
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
    lines = ["刚收到的记录还在后台完善分类，但已经可以查询："]
    for note in notes[:3]:
        content = note.get("text") or note.get("summary") or note.get("title") or ""
        lines.append(f"- {_clip(str(content), 220)}")
    return "\n".join(lines)


def _complete_json_with_hooks(
    context: AgentRunContext | None,
    *,
    name: str,
    system_prompt: str,
    user_prompt: str,
    model_role: str = "balanced",
    llm_task: str | None = None,
) -> dict[str, Any]:
    def call() -> dict[str, Any]:
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
            return answer

        observations: list[dict[str, Any]] = []
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
                add_step(trace, "evidence_selected", output_summary={"ids": _result_ids(provisional)})
                add_step(trace, "rerank", output_summary={"strategy": "local_lexical_recency", "ids": _result_ids(provisional)})
                answer = _with_sources(_provisional_answer(provisional), observations)
                _log_final_answer(space_id, answer, source="provisional_read_after_write", observations=observations)
                add_step(trace, "answer_generated", output_summary={"answer_len": len(answer)}, reason="no_llm_wait")
                add_step(trace, "answer_returned", output_summary={"answer_len": len(answer)})
                finish_trace(trace)
                return answer

            fast_route = _deterministic_route(question)
            if fast_route is not None:
                action = str(fast_route["action"])
                args = dict(fast_route["args"])
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
                add_step(trace, "evidence_selected", output_summary={"ids": _result_ids(result)})
                add_step(trace, "rerank", output_summary={"strategy": "fast_path_tool_order", "ids": _result_ids(result)})
                if fast_route["synthesize"] and result:
                    answer = _synthesize_answer(question, observations, hook_context=hook_context)
                    reason = "fast_path_single_synthesis"
                else:
                    answer = _fallback_answer(observations)
                    reason = "fast_path_deterministic_answer"
                answer = _with_sources(answer, observations)
                _log_final_answer(space_id, answer, source=reason, observations=observations)
                add_step(trace, "answer_generated", output_summary={"answer_len": len(answer)}, reason=reason)
                add_step(trace, "answer_returned", output_summary={"answer_len": len(answer)})
                finish_trace(trace)
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
            memory_prefetch = _run_tool(
                space_id,
                "memory_search",
                {"query": question, "limit": 5, "min_score": DEFAULT_MEMORY_MIN_SCORE},
                trace=trace,
                hook_context=hook_context,
            )
            if memory_prefetch:
                observations.append(
                    {
                        "thought": "先召回最新 active 长期记忆。",
                        "tool": "memory_search",
                        "args": {"query_len": len(question), "limit": 5, "min_score": DEFAULT_MEMORY_MIN_SCORE},
                        "result": memory_prefetch,
                    }
                )
                add_step(trace, "evidence_selected", output_summary={"ids": _result_ids(memory_prefetch)})

            react_llm_task = "query_complex_reasoning" if any(marker in question for marker in _COMPLEX_QUERY_MARKERS) or max_steps > 2 else "query_routing"
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
                    answer = _with_sources(_fallback_answer(observations), observations)
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
                    return answer

                final_answer = str(decision.get("final_answer") or "").strip()
                if hook_context is not None and "session_update" in decision:
                    update = decision.get("session_update")
                    hook_context.metadata["session_update"] = update if isinstance(update, dict) else {}
                if final_answer and observations:
                    answer = _with_sources(final_answer, observations)
                    _log_final_answer(space_id, answer, source="react_final", observations=observations)
                    add_step(trace, "answer_generated", output_summary={"answer_len": len(final_answer)}, reason="react_final")
                    add_step(trace, "answer_returned", output_summary={"answer_len": len(answer)})
                    finish_trace(trace)
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
                add_step(trace, "evidence_selected", output_summary={"ids": _result_ids(result)})
                add_step(trace, "rerank", output_summary={"strategy": "tool_order", "ids": _result_ids(result)})

            answer = _with_sources(_synthesize_answer(question, observations, hook_context=hook_context), observations)
            _log_final_answer(space_id, answer, source="synthesized", observations=observations)
            add_step(trace, "answer_generated", output_summary={"answer_len": len(answer)}, reason="synthesized")
            add_step(trace, "answer_returned", output_summary={"answer_len": len(answer)})
            finish_trace(trace)
            return answer
        except Exception as exc:
            add_step(trace, "answer_failed", status="failed", error=str(exc))
            finish_trace(trace, status="failed")
            raise


def answer_question(
    space_id: str,
    question: str,
    max_steps: int = 4,
    *,
    tenant_id: str = "default",
    user_id: str | None = None,
    message_id: str | None = None,
    task_id: str | None = None,
) -> str:
    context = AgentRunContext.create(
        space_id=space_id,
        run_type="query",
        tenant_id=tenant_id,
        user_id=user_id,
        message_id=message_id,
        task_id=task_id,
        metadata={"question_len": len(question), "max_steps": max_steps},
    )
    return get_default_hook_manager().run_agent(
        context,
        lambda: _answer_question_impl(space_id, question, max_steps, context),
    )
