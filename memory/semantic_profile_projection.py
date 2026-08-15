"""Semantic profile projection service.

The projection is derived, per-facet state.  It can be rebuilt from append-only
semantic memories and must never mutate them.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from core.settings import (
    SEMANTIC_PROFILE_FINAL_INPUT_CHAR_BUDGET,
    SEMANTIC_PROFILE_PROJECTION_DEBOUNCE_SECONDS,
    SEMANTIC_PROFILE_PROJECTION_INPUT_CHAR_BUDGET,
    SEMANTIC_PROFILE_PROJECTION_MAX_FACTS_PER_FACET,
)
from memory.models import MemoryRecord
from repositories.postgres.semantic_profile_projection import (
    get_semantic_profile_projection,
    get_semantic_profile_projections,
    record_semantic_profile_projection_error,
    save_semantic_profile_projection,
)

LOGGER = logging.getLogger(__name__)

_FACET_PROJECTION_PROMPT = """你是随心记的 semantic 分类画像投影器。原始事实是追加式证据，不能删除或改写。
只输出 JSON：
{"summary":"不超过160字、基于事实的该类当前概览","current_memory_ids":["..."],"uncertain_memory_ids":["..."]}

规则：
- 只能使用输入中真实存在的 memory id；不得编造事实。
- 可同时保留互不冲突的多个事实，尤其是 career、project、learning、capability。
- 只有明确时间顺序或明确变化证据时，才把旧事实从 current 移到 uncertain。
- 无法判断替代关系时，不能删除，放入 uncertain。
- summary 只能概括 current_memory_ids 和 uncertain_memory_ids 对应内容，不得新增信息。
"""

_FINAL_PROFILE_PROMPT = """你是随心记的用户画像汇总器。输入是每个 semantic facet 已审核的摘要，以及可能比摘要更新的原始增量事实。
只输出 JSON：{"profile_lines":["..."],"uncertain_lines":["..."]}。

规则：
- profile_lines 最多 10 条，每条不超过100字；uncertain_lines 最多5条。
- 增量事实比对应 facet 摘要更新，必须纳入本次结果；不得遗漏。
- 只能重组输入事实，不得补充、推测、夸大或生成输入没有的结论。
- 明确时间顺序支持替代时展示较新结论；无法确认时放入 uncertain_lines。
"""


def _memory_payload(memory: MemoryRecord) -> dict[str, Any]:
    return {
        "id": memory.id,
        "facet": memory.predicate or "other",
        "observed_at": memory.updated_at,
        "content": memory.content,
        "source_note_ids": [source.note_id for source in memory.sources if source.note_id][:12],
        "source_count": len(memory.sources),
    }


def _ordered_facts(memories: list[MemoryRecord]) -> list[MemoryRecord]:
    return sorted(
        memories,
        key=lambda memory: (
            str(memory.updated_at or ""),
            float(memory.importance or 0.0),
            float(memory.confidence or 0.0),
            memory.id,
        ),
        reverse=True,
    )


def _bounded_fact_payload(
    memories: list[MemoryRecord],
    *,
    char_budget: int,
    max_facts: int,
) -> tuple[list[dict[str, Any]], int]:
    payload: list[dict[str, Any]] = []
    used = 0
    for memory in _ordered_facts(memories):
        if len(payload) >= max(1, int(max_facts)):
            break
        item = _memory_payload(memory)
        content = str(item["content"])
        remaining = max(0, int(char_budget) - used)
        if remaining <= 0:
            break
        if len(content) > min(480, remaining):
            item["content"] = content[: min(480, remaining)] + "…"
        item_size = len(str(item["content"]))
        if item_size > remaining:
            break
        payload.append(item)
        used += item_size
    return payload, max(0, len(memories) - len(payload))


def _valid_ids(values: Any, known: set[str], *, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(
        str(value) for value in values if str(value) in known
    ))[:limit]


def _fallback_summary(facts: list[MemoryRecord]) -> str:
    return "；".join(memory.content.strip() for memory in facts[:4] if memory.content.strip())[:500]


def _safe_lines(values: Any, *, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    lines: list[str] = []
    for value in values:
        line = str(value or "").strip().replace("\n", " ")
        if line and len(line) <= 140:
            lines.append(line)
    return list(dict.fromkeys(lines))[:limit]


def refresh_semantic_profile_projection(space_id: str, facet: str, memories: list[MemoryRecord] | None = None) -> bool:
    """Build one facet projection against its current target revision."""
    record = get_semantic_profile_projection(space_id, facet)
    if record is None:
        return False
    target_revision = int(record["target_revision"])
    if int(record["processed_revision"]) >= target_revision:
        return True
    if memories is None:
        from memory.repository import list_memories
        memories = list_memories(space_id, status="active", memory_type="semantic", limit=100)
    facts = _ordered_facts([
        memory for memory in memories if str(memory.predicate or "other") == facet
    ])
    packed_facts, truncated_fact_count = _bounded_fact_payload(
        facts,
        char_budget=SEMANTIC_PROFILE_PROJECTION_INPUT_CHAR_BUDGET,
        max_facts=SEMANTIC_PROFILE_PROJECTION_MAX_FACTS_PER_FACET,
    )
    known = {str(item["id"]) for item in packed_facts}
    if not facts:
        projection = {"summary": "", "current_memory_ids": [], "uncertain_memory_ids": [], "fact_count": 0}
    else:
        from core.llm_client import complete_json
        data = complete_json(
            system_prompt=_FACET_PROJECTION_PROMPT,
            user_prompt=json.dumps({"facet": facet, "facts": packed_facts, "truncated_fact_count": truncated_fact_count}, ensure_ascii=False),
            model_role="fast",
            llm_task="summary_draft",
        )
        current_ids = _valid_ids(data.get("current_memory_ids"), known, limit=12)
        uncertain_ids = _valid_ids(data.get("uncertain_memory_ids"), known, limit=8)
        if not current_ids and not uncertain_ids:
            current_ids = [str(item["id"]) for item in packed_facts[:8]]
        selected = [memory for memory in facts if memory.id in set([*current_ids, *uncertain_ids])]
        summary = str(data.get("summary") or "").strip().replace("\n", " ")
        projection = {
            "summary": summary[:500] or _fallback_summary(selected or facts),
            "current_memory_ids": current_ids,
            "uncertain_memory_ids": uncertain_ids,
            "fact_count": len(facts),
            "truncated_fact_count": truncated_fact_count,
        }
    return save_semantic_profile_projection(
        space_id,
        facet,
        expected_revision=target_revision,
        projection=projection,
        source_memory_ids=[memory.id for memory in facts],
    )


def semantic_projection_wait_seconds(space_id: str, facet: str) -> float:
    record = get_semantic_profile_projection(space_id, facet)
    if record is None or int(record["processed_revision"]) >= int(record["target_revision"]):
        return 0.0
    dirty_since = record.get("dirty_since")
    if not dirty_since:
        return 0.0
    try:
        started = datetime.fromisoformat(str(dirty_since).replace("Z", "+00:00"))
        elapsed = (datetime.now(started.tzinfo) - started).total_seconds()
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, float(SEMANTIC_PROFILE_PROJECTION_DEBOUNCE_SECONDS) - elapsed)


def refresh_semantic_projection_task(space_id: str, facet: str) -> bool:
    try:
        return refresh_semantic_profile_projection(space_id, facet)
    except Exception as exc:
        record_semantic_profile_projection_error(space_id, facet, f"{type(exc).__name__}: {exc}")
        raise


def semantic_profile_lines(space_id: str, memories: list[MemoryRecord]) -> tuple[list[str], list[str]] | None:
    """Return a compact profile view with a live, bounded delta overlay."""
    semantic = [memory for memory in memories if memory.memory_type == "semantic"]
    if not semantic:
        return [], []
    try:
        projections = get_semantic_profile_projections(space_id)
    except Exception:
        LOGGER.warning("semantic profile projection lookup failed", exc_info=True)
        return None
    if not projections:
        return None
    by_facet: dict[str, list[MemoryRecord]] = {}
    for memory in semantic:
        by_facet.setdefault(str(memory.predicate or "other"), []).append(memory)
    inputs: list[dict[str, Any]] = []
    fallback_lines: list[str] = []
    overflow_lines: list[str] = []
    remaining = max(512, int(SEMANTIC_PROFILE_FINAL_INPUT_CHAR_BUDGET))
    for facet, facts in sorted(by_facet.items()):
        record = projections.get(facet)
        projection = dict(record.get("projection") or {}) if record else {}
        summary = str(projection.get("summary") or "").strip()[:500]
        if summary:
            remaining = max(0, remaining - len(summary))
            fallback_lines.append(summary)
        source_ids = {str(value) for value in (record or {}).get("source_memory_ids") or []}
        stale = record is None or int(record.get("processed_revision") or 0) < int(record.get("target_revision") or 0)
        delta_memories = (
            _ordered_facts(facts)
            if record is None
            else _ordered_facts([memory for memory in facts if memory.id not in source_ids])
            if stale
            else []
        )
        delta_facts, truncated_delta_count = _bounded_fact_payload(
            delta_memories,
            char_budget=remaining,
            max_facts=SEMANTIC_PROFILE_PROJECTION_MAX_FACTS_PER_FACET,
        )
        remaining = max(0, remaining - sum(len(str(item["content"])) for item in delta_facts))
        overflow = delta_memories[len(delta_facts):]
        # The newest overflow facts remain visible in deterministic fallback
        # output even when they cannot fit in the final LLM budget.
        overflow_lines.extend(f"【最新待投影】{memory.content}" for memory in overflow[:3])
        fallback_lines.extend(f"【最新】{memory.content}" for memory in delta_memories[:5])
        inputs.append({
            "facet": facet,
            "summary": summary or None,
            "current_memory_ids": projection.get("current_memory_ids") or [],
            "uncertain_memory_ids": projection.get("uncertain_memory_ids") or [],
            "delta_facts": delta_facts,
            "delta_truncated_count": truncated_delta_count,
            "stale": stale,
        })
    try:
        from core.llm_client import complete_json
        data = complete_json(
            system_prompt=_FINAL_PROFILE_PROMPT,
            user_prompt=json.dumps({"facets": inputs}, ensure_ascii=False),
            model_role="fast",
            llm_task="summary_draft",
        )
        lines = _safe_lines(data.get("profile_lines"), limit=10)
        uncertain = _safe_lines(data.get("uncertain_lines"), limit=5)
        if lines or uncertain:
            return list(dict.fromkeys([*lines, *overflow_lines]))[:10], uncertain
    except Exception:
        LOGGER.warning("semantic profile final projection fallback", exc_info=True)
    return list(dict.fromkeys([*fallback_lines, *overflow_lines]))[:10], []
