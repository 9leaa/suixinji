"""Adjudicate the relationship between a candidate and current memories."""

from __future__ import annotations

import hashlib
import re

from core.settings import MEMORY_AUTO_MUTATION_MIN_CONFIDENCE
from memory.candidate_retriever import candidate_similarity
from memory.models import MemoryCandidate, MemoryDecision, MemoryRecord
from memory.policies import preference as preference_policy
from memory.policies import semantic as semantic_policy
from memory.policies import task as task_policy


DESTRUCTIVE_ACTIONS = {"merge", "update_task", "supersede", "conflict"}


def _combined_confidence(candidate: MemoryCandidate, relation_confidence: float) -> float:
    return 0.6 * float(candidate.confidence) + 0.4 * relation_confidence


def _safe_evidence(candidate: MemoryCandidate, memories: list[MemoryRecord]) -> list[str]:
    evidence = [f"note:{candidate.note_id}"] if candidate.note_id else []
    evidence.extend(f"memory:{memory.id}" for memory in memories[:8])
    return evidence


def _decision(
    candidate: MemoryCandidate,
    relation: str,
    action: str,
    confidence: float,
    reason: str,
    targets: list[MemoryRecord] | None = None,
) -> MemoryDecision:
    target_memories = targets or []
    bounded_confidence = min(1.0, max(0.0, float(confidence)))
    recommended_action = action
    if action in DESTRUCTIVE_ACTIONS and bounded_confidence < MEMORY_AUTO_MUTATION_MIN_CONFIDENCE:
        recommended_action = "pending_review"
        reason = f"{reason}; below_auto_mutation_threshold"
    input_hash = hashlib.sha256(
        "|".join([candidate.effective_memory_key, candidate.normalized_content, *[memory.id for memory in target_memories]]).encode("utf-8")
    ).hexdigest()[:16]
    return MemoryDecision(
        candidate_id=candidate.candidate_id,
        relation=relation,
        target_memory_ids=[memory.id for memory in target_memories],
        confidence=bounded_confidence,
        reason=reason,
        evidence=_safe_evidence(candidate, target_memories),
        recommended_action=recommended_action,
        input_hash=input_hash,
        target_snapshot_version=target_memories[0].current_version if target_memories else None,
    )


def _shares_topic(candidate: MemoryCandidate, memory: MemoryRecord, similarity: float) -> bool:
    if candidate.effective_memory_key == memory.effective_memory_key:
        return True
    if candidate.memory_type == "preference" and memory.memory_type == "preference":
        return (
            (
                preference_policy.topic_compatibility(candidate, memory) >= 0.75
                or preference_policy.is_comparative_alternative(candidate.content, memory.content)
            )
            and preference_policy.scopes_compatible(candidate, memory)
        )
    if candidate.memory_type == "task":
        return False
    if candidate.predicate and memory.predicate and candidate.predicate == memory.predicate:
        if candidate.predicate in {"location", "learning_focus", "current_project"}:
            return True
    if candidate.subject and memory.subject:
        if candidate.subject.casefold() == memory.subject.casefold():
            return True
    if candidate.entities and any(entity and entity.casefold() in memory.content.casefold() for entity in candidate.entities):
        return True
    if any(marker in candidate.content for marker in ("学习", "学", "研究")) and any(
        marker in memory.content for marker in ("学习", "学", "研究")
    ):
        return True
    if "工作" in candidate.content and "工作" in memory.content:
        return True
    if any(marker in candidate.content for marker in ("搬到", "住在")) and any(marker in memory.content for marker in ("搬到", "住在")):
        return True
    return similarity >= 0.65


def _near_same(candidate: MemoryCandidate, memory: MemoryRecord, similarity: float) -> bool:
    left = candidate.normalized_content
    right = memory.normalized_content
    if left == right:
        return True
    if not left or not right:
        return False
    if candidate.effective_memory_key == memory.effective_memory_key and similarity >= 0.89:
        if candidate.memory_type == "preference":
            candidate_signature = preference_policy.preference_signature(candidate.content)
            memory_signature = preference_policy.preference_signature(memory.content)
            return (
                candidate_signature.polarity == memory_signature.polarity
                and candidate_signature.qualifiers == memory_signature.qualifiers
                and candidate_signature.scopes == memory_signature.scopes
            )
        if candidate.memory_type == "task":
            return candidate.task_status == memory.task_status
        if candidate.memory_type == "semantic" and _semantic_conflict(candidate, memory):
            return False
        if candidate.memory_type == "semantic" and candidate.object_value and memory.object_value:
            return candidate.object_value.casefold() == memory.object_value.casefold()
        return True
    shorter, longer = sorted((left, right), key=len)
    return shorter in longer and len(shorter) / len(longer) >= 0.9


def _shares_named_token(candidate: MemoryCandidate, memory: MemoryRecord) -> bool:
    candidate_tokens = {token.casefold() for token in re.findall(r"[A-Za-z][A-Za-z0-9+#.-]*", candidate.content)}
    memory_tokens = {token.casefold() for token in re.findall(r"[A-Za-z][A-Za-z0-9+#.-]*", memory.content)}
    return bool(candidate_tokens & memory_tokens)


def _semantic_conflict(candidate: MemoryCandidate, memory: MemoryRecord) -> bool:
    if candidate.predicate == memory.predicate == "location":
        return not semantic_policy.explicitly_replaces(candidate.content, predicate="location")
    negative_markers = ("没有", "不是", "并非", "无问题", "不存在")
    candidate_negative = any(marker in candidate.content for marker in negative_markers)
    memory_negative = any(marker in memory.content for marker in negative_markers)
    return candidate_negative != memory_negative


def adjudicate_memory(candidate: MemoryCandidate, memories: list[MemoryRecord]) -> MemoryDecision:
    """Return an explainable decision; this function never writes to storage."""
    if not candidate.should_store:
        return _decision(candidate, "new", "discard", candidate.confidence, candidate.effective_reason or "candidate_should_not_store")
    if not memories:
        return _decision(candidate, "new", "insert", max(0.8, candidate.confidence), "no_related_active_memory")

    keyed_memories = [memory for memory in memories if candidate.effective_memory_key == memory.effective_memory_key]
    if candidate.memory_type == "preference" and not keyed_memories:
        keyed_memories = [
            memory
            for memory in memories
            if preference_policy.is_comparative_alternative(candidate.content, memory.content)
        ]
    if candidate.memory_type in {"preference", "task"} and not keyed_memories:
        return _decision(candidate, "new", "insert", max(0.8, candidate.confidence), "different_memory_key")
    if keyed_memories:
        memories = keyed_memories

    if candidate.memory_type == "preference":
        compatible_memories = [
            memory
            for memory in memories
            if (
                preference_policy.topic_compatibility(candidate, memory) >= 0.75
                or preference_policy.is_comparative_alternative(candidate.content, memory.content)
            )
            and preference_policy.scopes_compatible(candidate, memory)
        ]
        if not compatible_memories:
            return _decision(
                candidate,
                "new",
                "insert",
                max(0.8, candidate.confidence),
                "different_preference_topic_or_scope",
            )
        memories = compatible_memories

    if candidate.memory_type == "task":
        compatible_memories = [
            memory
            for memory in memories
            if task_policy.identifiers_compatible(candidate.content, memory.content)
        ]
        if not compatible_memories:
            return _decision(
                candidate,
                "new",
                "insert",
                max(0.8, candidate.confidence),
                "different_task_identifier",
            )
        memories = compatible_memories

    best = max(memories, key=lambda memory: candidate_similarity(candidate, memory))
    similarity = candidate_similarity(candidate, best)
    same_topic = _shares_topic(candidate, best, similarity)

    if _near_same(candidate, best, similarity):
        return _decision(candidate, "same", "add_source", max(0.92, candidate.confidence), "same_or_near_duplicate", [best])

    if candidate.memory_type == "task" and same_topic and candidate.task_status != best.task_status:
        if task_policy.can_transition(best.task_status, candidate.task_status):
            confidence = _combined_confidence(candidate, 0.82 + 0.12 * similarity)
            return _decision(candidate, "update_task", "update_task", confidence, "valid_task_status_transition", [best])
        return _decision(candidate, "conflict", "pending_review", 0.7, "invalid_or_ambiguous_task_status_transition", [best])

    if candidate.memory_type == "preference" and same_topic:
        if preference_policy.is_ambiguous_conflict(candidate.content, best.content):
            return _decision(
                candidate,
                "conflict",
                "pending_review",
                _combined_confidence(candidate, max(0.82, similarity)),
                "ambiguous_preference_conflict",
                [best],
            )
        if preference_policy.explicitly_replaces(candidate.content, best.content):
            confidence = _combined_confidence(candidate, 0.82 + 0.12 * similarity)
            return _decision(candidate, "supersede", "supersede", confidence, "explicit_preference_change", [best])

    if candidate.memory_type == "semantic" and same_topic and _semantic_conflict(candidate, best):
        confidence = _combined_confidence(candidate, max(0.82, similarity))
        return _decision(candidate, "conflict", "pending_review", confidence, "semantic_contradiction_requires_review", [best])

    if candidate.memory_type == "semantic" and same_topic and semantic_policy.explicitly_replaces(
        candidate.content,
        predicate=candidate.predicate,
    ):
        confidence = _combined_confidence(candidate, 0.82 + 0.12 * similarity)
        return _decision(candidate, "supersede", "supersede", confidence, "explicit_semantic_change", [best])

    if same_topic and (similarity >= 0.34 or _shares_named_token(candidate, best)):
        confidence = _combined_confidence(candidate, 0.74 + 0.16 * similarity)
        return _decision(candidate, "merge", "merge", confidence, "compatible_extension", [best])

    return _decision(candidate, "new", "insert", max(0.78, candidate.confidence), "no_actionable_relation")
