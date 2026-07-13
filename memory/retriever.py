"""Memory retrieval scoring."""

from __future__ import annotations

from datetime import datetime

from memory.models import MemoryRecord, normalize_content


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _overlap_score(query: str, content: str) -> float:
    if any(marker in query for marker in ("住哪", "住在", "在哪", "哪里")) and any(marker in content for marker in ("住在", "搬到")):
        return 0.7
    q = set(normalize_content(query))
    c = set(normalize_content(content))
    if not q or not c:
        return 0.0
    return len(q & c) / len(q)


def _intent_score(query: str, memory: MemoryRecord) -> float:
    if memory.memory_type == "task" and any(marker in query for marker in ("待办", "任务", "要做", "进度")):
        return 0.7
    if memory.memory_type == "preference" and any(marker in query for marker in ("喜欢", "偏好", "习惯")):
        return 0.6
    return 0.0


def _recency_score(memory: MemoryRecord) -> float:
    updated = _parse_ts(memory.updated_at)
    if updated is None:
        return 0.0
    age_days = max(0.0, (datetime.now().astimezone() - updated).total_seconds() / 86400)
    if age_days <= 7:
        return 1.0
    if age_days >= 365:
        return 0.1
    return max(0.1, 1.0 - age_days / 365)


def score_memory(query: str, memory: MemoryRecord) -> float:
    semantic_similarity = max(_overlap_score(query, memory.content), _intent_score(query, memory))
    if semantic_similarity <= 0:
        return 0.0
    status_factor = 1.0
    if memory.status == "conflicted":
        status_factor = 0.5
    elif memory.status != "active":
        status_factor = 0.2
    access_frequency = min(memory.access_count / 10, 1.0)
    final = (
        0.45 * semantic_similarity
        + 0.20 * memory.importance
        + 0.15 * _recency_score(memory)
        + 0.10 * memory.confidence
        + 0.05 * semantic_similarity
        + 0.05 * access_frequency
    )
    return round(final * status_factor, 4)


def search(space_id: str, query: str, *, memory_type: str | None = None, limit: int = 10) -> list[dict[str, object]]:
    from memory.repository import search_memories

    return [
        {
            **memory.to_dict(),
            "score": score,
        }
        for memory, score in search_memories(space_id, query, memory_type=memory_type, limit=limit)
    ]
