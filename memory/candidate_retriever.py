"""Retrieve active memories that may relate to a candidate."""

from __future__ import annotations

from core.settings import MEMORY_ADJUDICATION_TOP_K
from memory.models import MemoryCandidate, MemoryRecord, normalize_content
from memory.policies import preference as preference_policy
from memory.policies import task as task_policy
from memory.repository import list_memories


def _char_similarity(left: str, right: str) -> float:
    left_set = set(normalize_content(left))
    right_set = set(normalize_content(right))
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def candidate_similarity(candidate: MemoryCandidate, memory: MemoryRecord) -> float:
    if candidate.normalized_content == memory.normalized_content:
        return 1.0

    # Retrieval happens before adjudication and is capped to a small top-k.  Do
    # not let a shared sentence template (or A1 being a substring of A10) push
    # the actual same-topic memory out of that list.
    if candidate.memory_type == memory.memory_type == "preference":
        same_topic = preference_policy.topic_compatibility(candidate, memory) >= 0.75
        comparative_alternative = preference_policy.is_comparative_alternative(candidate.content, memory.content)
        if not same_topic and not comparative_alternative:
            return 0.0
    if candidate.memory_type == memory.memory_type == "task" and not task_policy.identifiers_compatible(
        candidate.content,
        memory.content,
    ):
        return 0.0

    score = _char_similarity(candidate.content, memory.content) * 0.55
    if candidate.predicate and memory.predicate and candidate.predicate == memory.predicate:
        score += 0.35
    if candidate.subject and memory.subject and normalize_content(candidate.subject) == normalize_content(memory.subject):
        score += 0.30
    if candidate.object_value and memory.object_value:
        object_score = _char_similarity(candidate.object_value, memory.object_value)
        score += 0.20 * object_score
    if candidate.entities and any(entity and entity.casefold() in memory.content.casefold() for entity in candidate.entities):
        score += 0.25
    if any(marker in candidate.content for marker in ("搬到", "住在")) and any(marker in memory.content for marker in ("搬到", "住在")):
        score = max(score, 0.72)
    if candidate.memory_type == "task" and candidate.predicate == memory.predicate == "task":
        score = max(score, 0.45)
    return round(min(score, 1.0), 4)


def retrieve_candidates(space_id: str, candidate: MemoryCandidate, *, limit: int | None = None) -> list[MemoryRecord]:
    """Use type/status filtering plus structured and lexical similarity.

    The repository keeps a memory-vector table for an optional embedding provider;
    this deterministic path remains the safe fallback when no vector is available.
    """
    top_k = limit if limit is not None else MEMORY_ADJUDICATION_TOP_K
    memories = list_memories(space_id, status="active", memory_type=candidate.memory_type, limit=100)
    scored = [(memory, candidate_similarity(candidate, memory)) for memory in memories]
    scored.sort(key=lambda item: (item[1], item[0].updated_at), reverse=True)
    return [memory for memory, score in scored[: max(1, min(int(top_k), 20))] if score >= 0.18]
