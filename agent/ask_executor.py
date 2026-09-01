"""Deterministic execution of validated AskPlan query units."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from dataclasses import dataclass, field
from datetime import datetime
import time
import re
from typing import Any

from agent.ask_models import AskPlan, EvidenceItem, QueryUnit, UnitEvidenceBundle, UnitResolution
from core import settings
from agent.note_reranker import fuse_note_variants, rerank_note_records, select_answer_evidence
from memory.models import normalize_content
from memory.repository import list_memories
from memory.service import build_memory_query_spec, memory_search, task_status_search


@dataclass
class AskExecutionResult:
    bundles: list[UnitEvidenceBundle] = field(default_factory=list)
    records_by_unit: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    executed_tools: list[str] = field(default_factory=list)
    unit_errors: dict[str, str] = field(default_factory=dict)
    timed_out_units: list[str] = field(default_factory=list)
    elapsed_ms: int = 0


def _source_ids(item: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for source in item.get("sources") or []:
        note_id = source.get("note_id") if isinstance(source, dict) else None
        if note_id and str(note_id) not in result:
            result.append(str(note_id))
    return result


def _as_evidence(item: dict[str, Any], *, tool: str, role: str) -> EvidenceItem:
    item_id = str(item.get("id") or item.get("memory_id") or item.get("note_id") or "")
    source_kind = str(item.get("source_kind") or "")
    is_note = source_kind == "note" or bool(item.get("note_id")) or (
        not item.get("memory_type")
        and bool(item.get("text") or item.get("summary") or item.get("title"))
    )
    is_version = source_kind == "memory_version" or bool(item.get("version_id"))
    content = str(item.get("content") or item.get("text") or item.get("summary") or item.get("title") or "")[:2400]
    return EvidenceItem(
        evidence_id=item_id or f"tool:{tool}",
        source_kind="note" if is_note else ("memory_version" if is_version else "memory"),
        memory_type=item.get("memory_type"),
        content=content,
        evidence_span=str(item.get("evidence_span") or item.get("snippet") or "")[:1200] or None,
        memory_id=None if is_note else str(item.get("memory_id") or (None if is_version else item_id)) or None,
        version_id=str(item.get("version_id") or item_id) if is_version else None,
        note_id=item_id if is_note else None,
        source_note_ids=_source_ids(item),
        event_time=item.get("event_time") or item.get("valid_from"),
        observed_at=item.get("observed_at") or item.get("source_created_at"),
        recorded_at=item.get("updated_at") or item.get("created_at"),
        task_status=str(item.get("task_status") or "") or None,
        retrieval_channels=[tool],
        evidence_role=role,
    )


def _semantic_record_identity(item: dict[str, Any]) -> str:
    scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
    topic = normalize_content(str(scope.get("canonical_topic") or ""))
    if topic:
        return f"topic:{topic}"
    subject = normalize_content(str(item.get("subject") or ""))
    predicate = normalize_content(str(item.get("predicate") or ""))
    if predicate:
        return f"slot:{subject}:{predicate}"
    return f"key:{item.get('memory_key') or item.get('id') or ''}"


def _semantic_fact_time(item: dict[str, Any]) -> float:
    sources = item.get("sources") or []
    values = [item.get("valid_from"), item.get("last_confirmed_at")]
    values.extend(source.get("created_at") for source in sources if isinstance(source, dict))
    values.extend([item.get("updated_at"), item.get("created_at")])
    for value in values:
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
    return float("-inf")


def _semantic_temporal_order(items: list[dict[str, Any]], *, query: str) -> list[dict[str, Any]]:
    """Latest query-authorized evidence first, without deleting history."""
    from memory.retriever import _overlap_score

    def relevance(item: dict[str, Any]) -> float:
        scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
        return max(
            _overlap_score(query, str(value or ""))
            for value in (
                item.get("content"), item.get("object_value"), item.get("predicate"),
                scope.get("canonical_topic"),
            )
        )

    scored = [(item, relevance(item)) for item in items]
    relevant = [item for item, score in scored if score >= 0.45]
    candidates = relevant if len(relevant) >= 2 else items
    if len(candidates) <= 1:
        return items
    latest = max(candidates, key=lambda item: (_semantic_fact_time(item), str(item.get("id") or "")))
    remainder = sorted(
        (item for item in items if item is not latest),
        key=lambda item: (relevance(item), _semantic_fact_time(item), str(item.get("id") or "")),
        reverse=True,
    )
    return [latest, *remainder]


def _semantic_projection_order(
    space_id: str,
    facet: str | None,
    items: list[dict[str, Any]],
    *,
    prefer_current: bool = False,
    query: str = "",
) -> tuple[list[dict[str, Any]], set[str]]:
    if not facet:
        return (_semantic_temporal_order(items, query=query) if prefer_current else items), set()
    try:
        from repositories.postgres.semantic_profile_projection import get_semantic_profile_projection

        projection = get_semantic_profile_projection(space_id, facet) or {}
        payload = projection.get("projection") or {}
        current = {str(value) for value in payload.get("current_memory_ids") or []}
        uncertain = {str(value) for value in payload.get("uncertain_memory_ids") or []}
        if not current and not uncertain and prefer_current:
            ordered = _semantic_temporal_order(items, query=query)
        else:
            ordered = sorted(
                items,
                key=lambda item: (
                    1 if str(item.get("id") or "") in current else 0,
                    0 if str(item.get("id") or "") in uncertain else 1,
                    _semantic_fact_time(item),
                ),
                reverse=True,
            )
        return ordered, uncertain
    except Exception:
        return (_semantic_temporal_order(items, query=query) if prefer_current else items), set()


def _semantic_current_structured_lane(space_id: str, unit: QueryUnit, ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add projection-selected rows, never every row sharing a broad facet."""
    facet = normalize_content(unit.facet or "")
    if not facet:
        return ranked
    try:
        from repositories.postgres.semantic_profile_projection import get_semantic_profile_projection

        projection = get_semantic_profile_projection(space_id, facet) or {}
        payload = projection.get("projection") or {}
        projected_ids = {
            str(value)
            for value in [
                *(payload.get("current_memory_ids") or []),
                *(payload.get("uncertain_memory_ids") or []),
            ]
            if str(value)
        }
        if not projected_ids:
            return ranked
        structured = [
            memory.to_dict()
            for memory in list_memories(space_id, status="active", memory_type="semantic", limit=200)
            if memory.id in projected_ids and normalize_content(memory.predicate or "") == facet
        ]
    except Exception:
        return ranked
    if not structured:
        return ranked

    def observed_at(item: dict[str, Any]) -> str:
        sources = item.get("sources") or []
        source_times = [str(source.get("created_at") or "") for source in sources if isinstance(source, dict)]
        return max([str(item.get("updated_at") or ""), str(item.get("created_at") or ""), *source_times])

    structured.sort(key=observed_at, reverse=True)
    known = {str(item.get("id") or "") for item in structured}
    return [*structured, *(item for item in ranked if str(item.get("id") or "") not in known)]


def _inventory_limit(unit: QueryUnit) -> int:
    """Respect an explicit user request count without turning a list into an unbounded dump."""
    text = " ".join([unit.question, *unit.source_spans])
    match = re.search(r"(\d+|一|两|二|三|四|五|六|七|八|九|十)\s*(?:个|项|件|条|份)", text)
    if not match:
        return 5
    raw = match.group(1)
    chinese = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    return max(1, min(int(raw) if raw.isdigit() else chinese[raw], 8))


def _task_source_supports_inventory(space_id: str, item: dict[str, Any]) -> bool:
    """Exclude tasks whose own source says the extracted assertion is irrelevant or negated."""
    from agent.query_agent import get_note_for_evidence

    note_ids = _source_ids(item)
    if not note_ids:
        return True
    contradiction_markers = ("无关", "不是当前任务", "并非当前任务", "误提取", "不需要处理")
    for note_id in note_ids[:3]:
        try:
            note = get_note_for_evidence(space_id, note_id)
            source_text = str((note or {}).get("text") or (note or {}).get("summary") or "")
        except Exception:
            continue
        if any(marker in source_text for marker in contradiction_markers):
            return False
    return True


def _task_inventory_records(space_id: str, *, access_context: Any, limit: int = 12) -> list[dict[str, Any]]:
    """Read a bounded, ACL-filtered active task inventory with source-consistency checks."""
    from memory.access import memory_access_allowed

    rows = []
    # Apply candidate quality and ACL gates before the user-visible count;
    # otherwise the first N stale/invalid candidates can make a valid list empty.
    fetch_limit = max(limit * 3, 12)
    for memory in list_memories(space_id, status="active", memory_type="task", limit=fetch_limit):
        item = memory.to_dict()
        if access_context is not None and not memory_access_allowed(item, access_context):
            continue
        if not _task_source_supports_inventory(space_id, item):
            continue
        rows.append(item)
    return rows[:limit]


def _timeline_version_records(space_id: str, unit: QueryUnit, *, access_context: Any) -> list[dict[str, Any]]:
    """Locate a memory by canonical topic and expose its ordered versions as evidence."""
    from memory.repository import get_memory_timeline

    queries = list(dict.fromkeys(value for value in (unit.topic, unit.question) if str(value or "").strip()))
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for query in queries:
        for timeline in get_memory_timeline(space_id, query=str(query), limit=10, access_context=access_context):
            memory_id = str(timeline.get("memory_id") or timeline.get("id") or "")
            # Explicit source-side negation makes a version audit-only, not
            # answer-bearing timeline evidence.
            if not _task_source_supports_inventory(space_id, timeline):
                continue
            versions = timeline.get("versions") or []
            for version in versions:
                version_id = str(version.get("id") or version.get("version_id") or "")
                evidence_id = version_id or f"{memory_id}:v{version.get('version')}"
                # A prior version may record an explicitly negated/irrelevant
                # extraction. It is audit data, not answer-bearing history.
                version_text = str(version.get("content") or timeline.get("content") or "")
                if any(marker in version_text for marker in ("无关信息", "并非相关", "误提取")):
                    continue
                if not evidence_id or evidence_id in seen:
                    continue
                seen.add(evidence_id)
                result.append({
                    "id": evidence_id,
                    "memory_id": memory_id,
                    "version_id": version_id or evidence_id,
                    "source_kind": "memory_version",
                    "memory_type": timeline.get("memory_type"),
                    "content": version.get("content") or timeline.get("content") or "",
                    "task_status": version.get("task_status"),
                    "status": version.get("status"),
                    "updated_at": version.get("valid_from") or timeline.get("updated_at"),
                    "event_time": version.get("valid_from"),
                    "sources": [{"note_id": version.get("source_note_id")}] if version.get("source_note_id") else timeline.get("sources") or [],
                })
    # Direct topic lookup is precise but cannot cover every paraphrase.  Use
    # the existing access-filtered Hybrid retrieval only to locate candidate
    # Memory IDs, then expose their persisted versions as the actual evidence.
    candidate_ids: list[str] = []
    for query in queries:
        for hit in memory_search(space_id, query, limit=6, access_context=access_context):
            memory_id = str(hit.get("id") or "")
            if memory_id and memory_id not in candidate_ids:
                candidate_ids.append(memory_id)
    for memory_id in candidate_ids:
        for timeline in get_memory_timeline(space_id, memory_id=memory_id, limit=1, access_context=access_context):
            if not _task_source_supports_inventory(space_id, timeline):
                continue
            for version in timeline.get("versions") or []:
                version_id = str(version.get("id") or version.get("version_id") or "")
                evidence_id = version_id or f"{memory_id}:v{version.get('version')}"
                # A prior version may record an explicitly negated/irrelevant
                # extraction. It is audit data, not answer-bearing history.
                version_text = str(version.get("content") or timeline.get("content") or "")
                if any(marker in version_text for marker in ("无关信息", "并非相关", "误提取")):
                    continue
                if not evidence_id or evidence_id in seen:
                    continue
                seen.add(evidence_id)
                result.append({
                    "id": evidence_id,
                    "memory_id": memory_id,
                    "version_id": version_id or evidence_id,
                    "source_kind": "memory_version",
                    "memory_type": timeline.get("memory_type"),
                    "content": version.get("content") or timeline.get("content") or "",
                    "task_status": version.get("task_status"),
                    "status": version.get("status"),
                    "updated_at": version.get("valid_from") or timeline.get("updated_at"),
                    "event_time": version.get("valid_from"),
                    "sources": [{"note_id": version.get("source_note_id")}] if version.get("source_note_id") else timeline.get("sources") or [],
                })
    return result


def _query_variants(unit: QueryUnit) -> list[str]:
    """Use the user wording and the planner topic as bounded complementary recall lanes."""
    return list(dict.fromkeys(
        value for value in (unit.question, unit.topic) if str(value or "").strip()
    ))


def _unit_query_spec(unit: QueryUnit, query: str):
    predicate = unit.facet if unit.facet and unit.facet != "other" else None
    return build_memory_query_spec(
        query,
        memory_type=unit.memory_type,
        canonical_topic=unit.topic,
        predicate=predicate,
        time_mode=unit.time_mode,
    )


def _rerank_unit_records(unit: QueryUnit, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rerank candidates without allowing relevance to change domain semantics."""
    if not records or not getattr(settings, "ASK_CROSS_ENCODER_ENABLED", False):
        return records
    if unit.intent in {"task_inventory", "memory_history"}:
        return records
    limit = max(2, min(int(getattr(settings, "ASK_CROSS_ENCODER_TOP_K", 12)), len(records)))
    head, tail = records[:limit], records[limit:]
    try:
        from agent.cross_encoder_reranker import rerank_ask_records
        reranked = rerank_ask_records(
            unit.question,
            head,
            model_name=getattr(settings, "ASK_CROSS_ENCODER_MODEL", ""),
            proxy=getattr(settings, "ASK_CROSS_ENCODER_PROXY", "") or None,
            alpha=getattr(settings, "ASK_CROSS_ENCODER_ALPHA", 0.75),
        )
        return [*reranked, *tail]
    except Exception:
        return records


def _rerank_task_records(unit: QueryUnit, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current = [item for item in records if str(item.get("task_evidence_role") or "") == "current_task" or str(item.get("memory_type") or "") == "task"]
    history = [item for item in records if item not in current]
    return [*_rerank_unit_records(unit, current), *_rerank_unit_records(unit, history)]


def _rerank_preference_records(unit: QueryUnit, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active = [item for item in records if str(item.get("status") or "active") not in {"pending", "pending_review", "conflicted"}]
    review = [item for item in records if item not in active]
    return [*_rerank_unit_records(unit, active), *_rerank_unit_records(unit, review)]


def _merge_records(groups: list[list[dict[str, Any]]], *, limit: int) -> list[dict[str, Any]]:
    """Fuse query variants without allowing the first group to exhaust the pool."""
    target = max(1, int(limit))
    items: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}
    ranks: dict[str, dict[int, int]] = {}
    primary_order: list[str] = []
    for group_index, group in enumerate(groups):
        seen_in_group: set[str] = set()
        weight = 1.15 if group_index == 0 else 1.0
        for rank, item in enumerate(group, start=1):
            item_id = str(item.get("id") or item.get("memory_id") or "")
            key = item_id or str(item.get("content") or "")
            if not key or key in seen_in_group:
                continue
            seen_in_group.add(key)
            items.setdefault(key, item)
            ranks.setdefault(key, {})[group_index] = rank
            scores[key] = scores.get(key, 0.0) + weight / (60 + rank)
            if group_index == 0:
                primary_order.append(key)

    if not items:
        return []
    if len(groups) <= 1:
        return [items[key] for key in primary_order[:target]]

    # Preserve the strongest original-query evidence, then let all lanes
    # compete for the remaining half through weighted RRF.
    primary_floor = min(len(primary_order), max(1, target // 2))
    selected = list(dict.fromkeys(primary_order[:primary_floor]))
    # Give each additional lane one bounded opportunity to contribute. This
    # prevents a full first result page from making a rewrite a no-op.
    for group_index in range(1, len(groups)):
        available = [
            key for key in items
            if key not in selected and group_index in ranks.get(key, {})
        ]
        if available and len(selected) < target:
            selected.append(min(available, key=lambda key: ranks[key][group_index]))

    remaining = sorted(
        (key for key in items if key not in selected),
        key=lambda key: (
            scores.get(key, 0.0),
            0 in ranks.get(key, {}),
            -min(ranks.get(key, {0: 10_000}).values()),
            key,
        ),
        reverse=True,
    )
    selected.extend(remaining[: max(0, target - len(selected))])
    return [items[key] for key in selected[:target]]



_GENERIC_QUERY_NGRAMS = {
    "告诉", "诉我", "记录", "中的", "什么", "么是", "是什", "哪里", "在哪", "现在", "当前",
    "到底", "喜欢", "不喜", "喜不", "最喜", "我最", "那个", "这个", "怎么", "么样", "样了",
    # User/assistant references occur in nearly every record and question;
    # they identify the owner, not the fact being queried.
    "用户", "本人", "自己", "我的", "说话", "话者",
}


def _record_identity_text(item: dict[str, Any]) -> str:
    scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
    values = (
        item.get("content"), item.get("subject"), item.get("predicate"), item.get("object_value"),
        item.get("memory_key"), scope.get("canonical_topic"), scope.get("semantic_facet"),
    )
    return normalize_content(" ".join(str(value or "") for value in values))


def _record_is_exportable(item: dict[str, Any], access_context: Any) -> bool:
    from core.sensitive import contains_sensitive_data
    from memory.access import memory_access_allowed
    scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
    sensitivity = str(scope.get("sensitivity") or "normal").casefold()
    return (
        not contains_sensitive_data(str(item.get("content") or ""))
        and sensitivity not in {"sensitive", "secret", "restricted", "high"}
        and (access_context is None or memory_access_allowed(item, access_context))
    )


def _record_relevant_to_unit(unit: QueryUnit, item: dict[str, Any]) -> bool:
    """Hybrid rank makes candidates; identity-bearing overlap authorizes evidence."""
    # Facet is a controlled routing label (for example `other`), not user
    # evidence.  Mixing it into lexical matching creates accidental overlaps
    # such as `other` matching the `user` owner prefix in every record.
    query = normalize_content(" ".join(value for value in (unit.question, unit.topic) if value))
    identity = _record_identity_text(item)
    if not query or not identity:
        return False
    if query in identity or identity in query:
        return True
    if unit.intent == "preference_current":
        # Only a true preference inventory may search all preferences. A
        # concrete topic must still pass identity-bearing overlap.
        topic = normalize_content(unit.topic or "")
        generic_topics = {"", "偏好", "喜好", "喜欢", "不喜欢", "个人偏好", "爱好"}
        asks_inventory = bool(re.search(r"(?:喜欢|不喜欢).{0,2}(?:什么|哪些|哪种)", query))
        if topic in generic_topics and asks_inventory:
            return True
    terms = {
        query[index:index + 2]
        for index in range(max(0, len(query) - 1))
        if query[index:index + 2] not in _GENERIC_QUERY_NGRAMS
    }
    if any(term in identity for term in terms):
        return True
    location_query = any(marker in query for marker in ("住", "居住", "住址", "地址", "所在地"))
    location_identity = any(marker in identity for marker in ("住", "居住", "住址", "地址", "所在地", "搬到", "迁居"))
    if location_query and location_identity:
        return True
    # Current focus is a stable semantic relation with several everyday forms.
    # Treating this as an alias keeps the gate conservative while allowing
    # “在忙什么” to resolve “当前重点”, without inspecting the fact value.
    focus_query = any(marker in query for marker in ("忙", "重点", "focus", "主要工作"))
    focus_identity = any(marker in identity for marker in ("忙", "重点", "focus", "主要工作"))
    if focus_query and focus_identity:
        return True
    if unit.facet == "device":
        return any(marker in identity for marker in ("设备", "电脑", "手机", "键盘", "显示器", "耳机"))
    if unit.facet == "capability":
        return any(marker in identity for marker in ("语言", "技术栈", "编程", "擅长", "能力"))
    return False


def _filtered_records(unit: QueryUnit, records: list[dict[str, Any]], *, access_context: Any) -> list[dict[str, Any]]:
    return [item for item in records if _record_is_exportable(item, access_context) and _record_relevant_to_unit(unit, item)]


def _pending_preference_records(space_id: str, unit: QueryUnit, *, access_context: Any) -> list[dict[str, Any]]:
    rows = [memory.to_dict() for memory in list_memories(space_id, status=None, memory_type="preference", limit=200)]
    return [item for item in _filtered_records(unit, rows, access_context=access_context) if str(item.get("status") or "") in {"pending_review", "pending", "conflicted"}]


def _semantic_history_fallback_records(space_id: str, unit: QueryUnit, *, access_context: Any) -> list[dict[str, Any]]:
    from memory.repository import get_memory_timeline
    rows = [memory.to_dict() for memory in list_memories(space_id, status=None, memory_type="semantic", limit=200)]
    result: list[dict[str, Any]] = []
    for item in _filtered_records(unit, rows, access_context=access_context):
        if str(item.get("status") or "active") == "active":
            continue
        for timeline in get_memory_timeline(space_id, memory_id=str(item.get("id") or ""), limit=1, access_context=access_context):
            for version in timeline.get("versions") or []:
                version_id = str(version.get("id") or version.get("version_id") or "")
                fallback_id = f"{item.get('id')}:v{version.get('version')}"
                result.append({
                    "id": version_id or fallback_id, "memory_id": str(item.get("id") or ""),
                    "version_id": version_id or fallback_id, "source_kind": "memory_version",
                    "memory_type": "semantic", "content": version.get("content") or item.get("content") or "",
                    "status": version.get("status") or item.get("status"), "event_time": version.get("valid_from"),
                    "updated_at": version.get("valid_from") or item.get("updated_at"),
                    "sources": [{"note_id": version.get("source_note_id")}] if version.get("source_note_id") else item.get("sources") or [],
                })
    return result[:8]

def _execute_domain_tool(space_id: str, unit: QueryUnit, *, access_context: Any) -> tuple[str, list[dict[str, Any]]]:
    variants = _query_variants(unit)
    if unit.intent == "task_inventory":
        return "list_task_inventory", _task_inventory_records(
            space_id, access_context=access_context, limit=_inventory_limit(unit)
        )
    if unit.intent == "task_state":
        records = _merge_records([
            task_status_search(
                space_id, query, query_spec=_unit_query_spec(unit, query),
                limit=8, access_context=access_context,
            )
            for query in variants
        ], limit=8)
        return "search_task_state", _rerank_task_records(unit, records)
    if unit.intent == "preference_current":
        ranked = _merge_records([
            memory_search(
                space_id, query, memory_type="preference",
                query_spec=_unit_query_spec(unit, query), limit=6,
                access_context=access_context,
            )
            for query in variants
        ], limit=12)
        current = _filtered_records(unit, ranked, access_context=access_context)
        review = _pending_preference_records(space_id, unit, access_context=access_context)
        return "search_preferences", _rerank_preference_records(unit, _merge_records([current, review], limit=6))
    if unit.intent in {"semantic_current", "semantic_history"}:
        ranked = _merge_records([
            memory_search(
                space_id, query, memory_type="semantic",
                query_spec=_unit_query_spec(unit, query), limit=10,
                access_context=access_context,
            )
            for query in variants
        ], limit=12)
        ranked = _filtered_records(unit, ranked, access_context=access_context)
        ranked = _rerank_unit_records(unit, ranked)
        if unit.intent == "semantic_current":
            current = _filtered_records(unit, _semantic_current_structured_lane(space_id, unit, ranked), access_context=access_context)
            if current:
                return "resolve_semantic_facts", current
            return "resolve_semantic_history_fallback", _semantic_history_fallback_records(space_id, unit, access_context=access_context)
        return "resolve_semantic_facts", ranked
    if unit.intent == "episodic_history":
        records = _merge_records([
            memory_search(
                space_id, query, memory_type="episodic",
                query_spec=_unit_query_spec(unit, query), limit=10,
                access_context=access_context,
            )
            for query in variants
        ], limit=10)
        return "search_episodes", _rerank_unit_records(unit, records)
    if unit.intent == "memory_history":
        return "get_memory_timeline", _timeline_version_records(
            space_id, unit, access_context=access_context,
        )
    from agent.query_agent import memory_note_fallback
    per_variant_limit = max(8, min(int(getattr(settings, "ASK_NOTE_CANDIDATES_PER_VARIANT", 30)), 30))
    pool_limit = max(
        per_variant_limit,
        min(int(getattr(settings, "ASK_NOTE_CANDIDATE_POOL_LIMIT", 50)), 50),
    )
    variant_groups = [
        memory_note_fallback(space_id, query, limit=per_variant_limit) for query in variants
    ]
    records = fuse_note_variants(variant_groups, limit=pool_limit)
    records = rerank_note_records(unit.question, records)
    return "search_notes", _rerank_unit_records(unit, records)


def _resolve(unit: QueryUnit, records: list[dict[str, Any]], *, space_id: str, tool: str) -> UnitEvidenceBundle:
    if unit.intent in {"semantic_current", "semantic_history"}:
        records, uncertain_ids = _semantic_projection_order(
            space_id,
            unit.facet,
            records,
            prefer_current=unit.intent == "semantic_current",
            query=" ".join(value for value in (unit.question, unit.topic) if value),
        )
    else:
        uncertain_ids = set()
    if not records:
        return UnitEvidenceBundle(
            unit_id=unit.id,
            resolution=UnitResolution(status="not_found", reason_code="no_direct_evidence"),
        )

    historical = unit.evidence_mode == "timeline" or unit.intent in {"semantic_history", "episodic_history", "memory_history"}
    evidence: list[EvidenceItem] = []
    evidence_limit = 8 if unit.intent in {"task_inventory", "memory_history"} else max(1, int(getattr(settings, "ASK_EVIDENCE_PER_UNIT", 5)))
    if tool == "search_notes":
        records = select_answer_evidence(unit.question, records, limit=evidence_limit)
    evidence_records = records[:evidence_limit]
    seen_semantic_identities: set[str] = set()
    for index, item in enumerate(evidence_records):
        item_id = str(item.get("id") or item.get("memory_id") or "")
        role = "historical" if historical or str(item.get("source_kind") or "") == "memory_version" else "current_candidate"
        if unit.intent == "semantic_current":
            identity = _semantic_record_identity(item)
            if identity in seen_semantic_identities:
                role = "historical"
            else:
                seen_semantic_identities.add(identity)
        if item_id in uncertain_ids:
            role = "conflicting"
        evidence.append(_as_evidence(item, tool=tool, role=role))

    conflicting = [item.evidence_id for item in evidence if item.evidence_role == "conflicting"]
    status = "conflict" if conflicting and not any(item.evidence_role == "current_candidate" for item in evidence) else "resolved"
    if unit.intent == "note_lookup":
        for item in evidence:
            item.evidence_role = "raw_note"
    value = evidence[0].content if evidence else None
    return UnitEvidenceBundle(
        unit_id=unit.id,
        evidence=evidence,
        resolution=UnitResolution(
            status=status,
            value=value,
            reason_code="projection_uncertain" if status == "conflict" else "direct_domain_evidence",
            selected_evidence_ids=[item.evidence_id for item in evidence if item.evidence_role != "conflicting"],
            conflicting_evidence_ids=conflicting,
        ),
    )


def _ready_units(plan: AskPlan, completed: set[str]) -> list[QueryUnit]:
    return [
        unit for unit in plan.units
        if unit.id not in completed and all(parent in completed for parent in unit.depends_on)
    ]


def execute_ask_plan(space_id: str, plan: AskPlan, *, access_context: Any = None) -> AskExecutionResult:
    """Execute independent units concurrently within a bounded response budget.

    A running database/client call cannot safely be force-cancelled from Python.
    Once the budget is exhausted, its Unit becomes partial and the response
    returns; any already-running read-only future is ignored.
    """
    started = time.monotonic()
    timeout_seconds = max(0.1, float(getattr(settings, "ASK_EXECUTOR_TIMEOUT_SECONDS", 15)))
    deadline = started + timeout_seconds
    result = AskExecutionResult()
    completed: set[str] = set()
    max_workers = max(1, min(int(getattr(settings, "ASK_MAX_PARALLEL_UNITS", 3)), len(plan.units)))

    def consume(future: Any, unit: QueryUnit) -> None:
        try:
            tool, records = future.result()
        except Exception as exc:
            tool, records = "domain_error", []
            result.unit_errors[unit.id] = f"{type(exc).__name__}: {str(exc)[:180]}"
        result.executed_tools.append(tool)
        result.records_by_unit[unit.id] = records
        result.bundles.append(_resolve(unit, records, space_id=space_id, tool=tool))
        completed.add(unit.id)

    while len(completed) < len(plan.units):
        ready = _ready_units(plan, completed)
        if not ready:
            # The validator should prevent this; retain a safe per-unit result.
            for unit in plan.units:
                if unit.id not in completed:
                    result.bundles.append(UnitEvidenceBundle(
                        unit_id=unit.id,
                        resolution=UnitResolution(status="partial", reason_code="unresolved_dependency"),
                    ))
                    completed.add(unit.id)
            break
        pool = ThreadPoolExecutor(max_workers=max_workers)
        futures = {
            pool.submit(_execute_domain_tool, space_id, unit, access_context=access_context): unit
            for unit in ready
        }
        consumed: set[Any] = set()
        try:
            remaining = max(0.0, deadline - time.monotonic())
            for future in as_completed(futures, timeout=remaining):
                consume(future, futures[future])
                consumed.add(future)
        except TimeoutError:
            pass
        finally:
            for future, unit in futures.items():
                if future in consumed:
                    continue
                if future.done():
                    consume(future, unit)
                    continue
                future.cancel()
                result.unit_errors[unit.id] = "executor_timeout"
                result.timed_out_units.append(unit.id)
                result.records_by_unit[unit.id] = []
                result.executed_tools.append("executor_timeout")
                result.bundles.append(UnitEvidenceBundle(
                    unit_id=unit.id,
                    resolution=UnitResolution(status="partial", reason_code="executor_timeout"),
                ))
                completed.add(unit.id)
            pool.shutdown(wait=False, cancel_futures=True)
    order = {unit.id: index for index, unit in enumerate(plan.units)}
    result.bundles.sort(key=lambda bundle: order.get(bundle.unit_id, 999))
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result


def _note_fallback(space_id: str, query: str) -> list[dict[str, Any]]:
    from agent.query_agent import memory_note_fallback

    return memory_note_fallback(space_id, query, limit=5)


def _partial_bundle(unit: QueryUnit, records: list[dict[str, Any]], *, tool: str, reason: str) -> UnitEvidenceBundle:
    role = "historical" if unit.intent == "task_state" and tool == "search_episodes" else "raw_note"
    evidence = [_as_evidence(item, tool=tool, role=role) for item in records[:3]]
    return UnitEvidenceBundle(
        unit_id=unit.id,
        evidence=evidence,
        resolution=UnitResolution(
            status="partial",
            value=evidence[0].content if evidence else None,
            reason_code=reason,
            selected_evidence_ids=[item.evidence_id for item in evidence],
        ),
    )


def repair_missing_evidence(
    space_id: str,
    plan: AskPlan,
    result: AskExecutionResult,
    *,
    access_context: Any = None,
) -> list[str]:
    """Run one deterministic fallback round for a bounded set of missed units."""
    by_id = {bundle.unit_id: bundle for bundle in result.bundles}
    repaired: list[str] = []
    if int(getattr(settings, "ASK_MAX_RETRIEVAL_ROUNDS", 2)) < 2:
        return repaired
    budget = max(0, int(getattr(settings, "ASK_MAX_FALLBACK_UNITS", 2)))
    for unit in plan.units:
        if len(repaired) >= budget:
            break
        bundle = by_id.get(unit.id)
        if bundle is None or bundle.resolution.status != "not_found":
            continue
        records: list[dict[str, Any]] = []
        tool = "search_notes"
        reason = "note_fallback"
        try:
            if unit.intent == "task_state":
                records = memory_search(
                    space_id, unit.question, memory_type="episodic", limit=4, access_context=access_context,
                )
                tool = "search_episodes"
                reason = "task_episode_fallback"
                if not records:
                    records = _note_fallback(space_id, unit.question)
                    tool = "search_notes"
                    reason = "task_note_fallback"
            else:
                records = _note_fallback(space_id, unit.question)
        except Exception:
            records = []
        # A fallback Note is evidence only when it still bears the Unit's
        # identity. Its retrieval score alone must never turn an unrelated
        # note into a partial answer.
        records = [item for item in records if _record_relevant_to_unit(unit, item)]
        if not records:
            continue
        replacement = _partial_bundle(unit, records, tool=tool, reason=reason)
        index = result.bundles.index(bundle)
        result.bundles[index] = replacement
        result.records_by_unit[unit.id] = records
        result.executed_tools.append(tool)
        repaired.append(unit.id)
    return repaired


def hydrate_selected_evidence(
    space_id: str,
    plan: AskPlan,
    result: AskExecutionResult,
) -> list[dict[str, Any]]:
    """One bounded source-note expansion for units which explicitly need it."""
    need_sources = {unit.id for unit in plan.units if unit.need_source_evidence}
    if not need_sources:
        return []
    max_ids = max(1, int(getattr(settings, "ASK_MAX_HYDRATE_IDS", 5)))
    note_ids: list[str] = []
    for bundle in result.bundles:
        if bundle.unit_id not in need_sources:
            continue
        for item in bundle.evidence:
            if item.source_kind == "note" and item.note_id and item.note_id not in note_ids:
                note_ids.append(item.note_id)
                if len(note_ids) >= max_ids:
                    break
            for note_id in item.source_note_ids:
                if note_id not in note_ids:
                    note_ids.append(note_id)
                if len(note_ids) >= max_ids:
                    break
    if not note_ids:
        return []
    from agent.query_agent import get_note_for_evidence

    notes: list[dict[str, Any]] = []
    for note_id in note_ids:
        note = get_note_for_evidence(space_id, note_id)
        if not note or note.get("error"):
            continue
        # Return only a bounded preview to trace/UI callers.  The full text is
        # held on the EvidenceItem briefly and is reduced by EvidenceResolver.
        notes.append({
            "id": note.get("id") or note_id,
            "title": note.get("title"),
            "summary": note.get("summary"),
            "text": str(note.get("text") or "")[:1200],
            "ts": note.get("ts") or note.get("created_at"),
        })
        for bundle in result.bundles:
            if bundle.unit_id not in need_sources:
                continue
            matching = [
                item for item in bundle.evidence
                if item.note_id == note_id or item.evidence_id == note_id or note_id in item.source_note_ids
            ]
            if not matching:
                continue
            for item in matching:
                item.full_text = str(note.get("text") or note.get("summary") or note.get("title") or "")
                item.observed_at = item.observed_at or note.get("ts") or note.get("created_at")
                if "expand_memory_evidence" not in item.retrieval_channels:
                    item.retrieval_channels.append("expand_memory_evidence")
            # A Memory source expands to a Note which is a distinct citation.
            if not any(item.note_id == note_id for item in bundle.evidence):
                bundle.evidence.append(EvidenceItem(
                    evidence_id=str(note.get("id") or note_id),
                    source_kind="note",
                    content=str(note.get("summary") or note.get("title") or "")[:2400],
                    full_text=str(note.get("text") or note.get("summary") or note.get("title") or ""),
                    note_id=str(note.get("id") or note_id),
                    observed_at=note.get("ts") or note.get("created_at"),
                    retrieval_channels=["expand_memory_evidence"],
                    evidence_role="raw_note",
                ))
    return notes
