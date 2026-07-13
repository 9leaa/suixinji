"""Query ReAct agent for searching and reasoning over stored notes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from core.llm_client import complete_json, embed_text
from core.observability import log_event, observe
from core.settings import QUERY_MIN_SCORE, QUERY_TOP_K
from core.taxonomy import is_valid_tag, is_valid_type, normalize_tag, normalize_type
from storage.note_storage import load_index
from storage.vector_store import search_related


DEFAULT_QUERY_MIN_SCORE = QUERY_MIN_SCORE


REACT_SYSTEM_PROMPT = f"""
你是“随心记 Agent”的查询助手。

你可以使用工具查询用户的历史笔记，然后回答问题。

可用工具：
1. filter_notes(type, tags, match_all_tags, limit): 当用户明确给出固定 type 或固定 tags 条件时，直接筛选 index.json。
2. semantic_search(query, top_k, min_score): 当用户没有明确 type/tags，或忘记分类条件时，按语义搜索相关笔记。
3. list_recent(days, limit): 查看最近若干天笔记。
4. get_note(note_id): 按 id 读取一条完整笔记。
5. follow_links(note_id, limit): 查看某条笔记 related 关联的笔记，包括它指向的笔记和指向它的笔记。

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
- 如果用户问“和某条笔记相关的有哪些”，先 semantic_search 找候选 note_id，再 follow_links。
- 回答只能基于 observations，不要编造。
- 如果没有找到相关笔记，要明确说没找到。
- 回答要自然、简洁，必要时引用笔记标题或时间。
"""

FINAL_SYSTEM_PROMPT = """
你是“随心记 Agent”的最终回答器。

请只基于给定 observations 回答用户问题。
必须输出 JSON object：
{"final_answer":"..."}
"""


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


def _safe_tool_args(action: str, args: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {"tool": action}
    for key in ("type", "note_type", "tags", "tag", "limit", "top_k", "min_score", "days", "note_id", "match_all_tags"):
        if key in args:
            safe[key] = args.get(key)
    if "query" in args:
        safe["query_len"] = len(str(args.get("query") or ""))
    return safe


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
    }


def _find_note(space_id: str, note_id: str) -> dict[str, Any] | None:
    for note in load_index(space_id):
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

    results = []
    for note in load_index(space_id):
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


def get_note(space_id: str, note_id: str) -> dict[str, Any]:
    note = _find_note(space_id, note_id)
    if note is None:
        return {"error": f"note not found: {note_id}"}
    return _note_brief(note, text_limit=1200)


def list_recent(space_id: str, days: int = 7, limit: int = 10) -> list[dict[str, Any]]:
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 30))

    now = datetime.now().astimezone()
    cutoff = now - timedelta(days=days)

    notes = []
    for note in load_index(space_id):
        ts = _parse_ts(note.get("ts"))
        if ts is not None and ts >= cutoff:
            notes.append(note)

    notes.sort(key=lambda item: item.get("ts", ""), reverse=True)
    return [_note_brief(note) for note in notes[:limit]]


def follow_links(space_id: str, note_id: str, limit: int = 5) -> dict[str, Any]:
    limit = max(1, min(int(limit), 20))
    notes = load_index(space_id)
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

    return {"error": f"unknown tool: {action}"}


def _run_tool(space_id: str, action: str, args: dict[str, Any]) -> Any:
    with observe(
        "query.tool_call",
        space_id=space_id,
        extra=_safe_tool_args(action, args),
    ):
        return _execute_tool(space_id, action, args)


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
        summary = item.get("summary") or item.get("text") or ""
        lines.append(f"- {title}：{summary}")
    return "\n".join(lines)


def _synthesize_answer(question: str, observations: list[dict[str, Any]]) -> str:
    payload = {
        "question": question,
        "observations": observations,
    }

    try:
        data = complete_json(
            system_prompt=FINAL_SYSTEM_PROMPT,
            user_prompt=json.dumps(payload, ensure_ascii=False, indent=2),
        )
    except Exception:
        return _fallback_answer(observations)

    return str(data.get("final_answer") or "").strip() or _fallback_answer(observations)


def answer_question(space_id: str, question: str, max_steps: int = 4) -> str:
    question = question.strip()
    with observe(
        "query.answer_question",
        space_id=space_id,
        extra={"question_len": len(question), "max_steps": max_steps},
    ):
        if not question:
            answer = "你想问什么？可以这样发：/ask 上次说的那件事是什么"
            _log_final_answer(space_id, answer, source="empty_question")
            return answer

        observations: list[dict[str, Any]] = []

        for step in range(max_steps):
            payload = {
                "question": question,
                "step": step + 1,
                "observations": observations,
            }

            decision = complete_json(
                system_prompt=REACT_SYSTEM_PROMPT,
                user_prompt=json.dumps(payload, ensure_ascii=False, indent=2),
            )

            final_answer = str(decision.get("final_answer") or "").strip()
            if final_answer and observations:
                _log_final_answer(space_id, final_answer, source="react_final", observations=observations)
                return final_answer

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

            result = _run_tool(space_id, str(action), args)
            observations.append(
                {
                    "thought": decision.get("thought"),
                    "tool": action,
                    "args": args,
                    "result": result,
                }
            )

        answer = _synthesize_answer(question, observations)
        _log_final_answer(space_id, answer, source="synthesized", observations=observations)
        return answer
