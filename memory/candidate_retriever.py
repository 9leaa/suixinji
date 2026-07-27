"""Retrieve active memories that may relate to a candidate."""

from __future__ import annotations

from core.settings import MEMORY_ADJUDICATION_TOP_K, MEMORY_RETRIEVAL_MODE
from memory.canonicalizer import task_identity_compatible
from memory.models import MemoryCandidate, MemoryRecord, normalize_content
from memory.policies import preference as preference_policy
from memory.policies import task as task_policy
from memory.relation_guard import is_v3_candidate
from memory.repository import hybrid_adjudication_candidates, list_adjudication_candidates


def _char_similarity(left: str, right: str) -> float:
    left_set = set(normalize_content(left))
    right_set = set(normalize_content(right))
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def candidate_similarity(candidate: MemoryCandidate, memory: MemoryRecord) -> float:
    exact_key = candidate.effective_memory_key == memory.effective_memory_key
    if exact_key:
        return 1.0
    if candidate.normalized_content == memory.normalized_content:
        return 1.0

    if is_v3_candidate(candidate):
        # V3 retrieval may still return broad semantic candidates, but identity
        # slots rather than a shared user/template decide their usefulness.
        if candidate.memory_type != memory.memory_type:
            return 0.0
        if candidate.memory_type == "task":
            if task_identity_compatible(candidate, memory):
                return 0.88
            # A task's normal identity is its exact canonical key.  The one
            # intentional exception is a later title that strictly refines an
            # existing short title ("简历" -> "Agent 开发的简历").  Keep that
            # candidate in the small adjudication set; the Relation Guard is
            # still the only component allowed to approve the mutation.
            candidate_operation = normalize_content(str(candidate.scope.get("operation") or ""))
            memory_operation = normalize_content(str(memory.scope.get("operation") or ""))
            candidate_scope = normalize_content(str(candidate.scope.get("scope") or "global"))
            memory_scope = normalize_content(str(memory.scope.get("scope") or "global"))
            candidate_title = normalize_content(candidate.predicate or "")
            memory_title = normalize_content(memory.predicate or "")
            is_strict_suffix_refinement = (
                candidate.subject
                and memory.subject
                and normalize_content(candidate.subject) == normalize_content(memory.subject)
                and candidate_operation == memory_operation
                and candidate_scope == memory_scope
                and len(memory_title) >= 2
                and len(candidate_title) > len(memory_title)
                and candidate_title.endswith(memory_title)
            )
            if is_strict_suffix_refinement:
                return 0.86
            return 0.0
        if candidate.memory_type == "semantic":
            if normalize_content(candidate.predicate or "") in {"fact", "事实"}:
                return 0.0
            if not (
                candidate.subject
                and memory.subject
                and candidate.predicate
                and memory.predicate
                and normalize_content(candidate.subject) == normalize_content(memory.subject)
                and normalize_content(candidate.predicate) == normalize_content(memory.predicate)
            ):
                return 0.0
        if candidate.memory_type == "preference":
            if preference_policy.topic_compatibility(candidate, memory) < 0.75:
                return 0.0

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


def retrieval_signals(candidate: MemoryCandidate, memory: MemoryRecord) -> dict[str, object]:
    """Compact, auditable explanation of a write-side retrieval candidate."""
    reasons: list[str] = []
    exact_key = candidate.effective_memory_key == memory.effective_memory_key
    if exact_key:
        reasons.append("exact_canonical_key")
    type_match = candidate.memory_type == memory.memory_type
    if type_match:
        reasons.append("memory_type_match")
    entity_match = bool(candidate.subject and memory.subject and normalize_content(candidate.subject) == normalize_content(memory.subject))
    if entity_match:
        reasons.append("entity_match")
    attribute_match = bool(candidate.predicate and memory.predicate and normalize_content(candidate.predicate) == normalize_content(memory.predicate))
    if attribute_match:
        reasons.append("attribute_match")
    operation_match = bool(
        candidate.scope.get("operation")
        and memory.scope.get("operation")
        and normalize_content(str(candidate.scope["operation"])) == normalize_content(str(memory.scope["operation"]))
    )
    if operation_match:
        reasons.append("operation_match")
    score = candidate_similarity(candidate, memory)
    if score:
        reasons.append("retrieval_score")
    return {
        "memory_id": memory.id,
        "exact_key": exact_key,
        "type_match": type_match,
        "entity_match": entity_match,
        "attribute_match": attribute_match,
        "operation_match": operation_match,
        "final_score": score,
        "reasons": reasons,
    }


def retrieve_candidates(space_id: str, candidate: MemoryCandidate, *, limit: int | None = None) -> list[MemoryRecord]:
    """Use hybrid retrieval when available, with deterministic scoring as fallback."""
    top_k = limit if limit is not None else MEMORY_ADJUDICATION_TOP_K
    if MEMORY_RETRIEVAL_MODE == "hybrid":
        memories = hybrid_adjudication_candidates(space_id, candidate, limit=max(top_k * 3, 20))
    else:
        memories = list_adjudication_candidates(
            space_id,
            memory_type=candidate.memory_type,
            memory_key=candidate.effective_memory_key,
            limit=200,
        )
    scored = [(memory, candidate_similarity(candidate, memory)) for memory in memories]
    scored.sort(key=lambda item: (item[1], item[0].updated_at), reverse=True)
    return [memory for memory, score in scored[: max(1, min(int(top_k), 20))] if score >= 0.18]
