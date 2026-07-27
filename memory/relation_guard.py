"""Local, deterministic approval guard for Memory V3 mutations."""

from __future__ import annotations

from dataclasses import dataclass

from memory.canonicalizer import task_identity_compatible
from memory.models import MEMORY_KEY_V3_VERSION, MemoryCandidate, MemoryRecord, normalize_content
from memory.policies import preference as preference_policy
from memory.policies import task as task_policy


@dataclass(frozen=True)
class RelationGuardResult:
    relation: str
    action: str
    reason: str
    approved: bool


def _same(left: str | None, right: str | None) -> bool:
    return bool(left and right and normalize_content(left) == normalize_content(right))


def _scope(memory: MemoryRecord, key: str, default: str = "") -> str:
    return str(memory.scope.get(key) or default)


def is_v3_candidate(candidate: MemoryCandidate) -> bool:
    return candidate.memory_key_version == MEMORY_KEY_V3_VERSION or candidate.scope.get("memory_key_version") == MEMORY_KEY_V3_VERSION


def _task_values_changed(candidate: MemoryCandidate, memory: MemoryRecord) -> bool:
    """Whether a same-key task carries a concrete replacement-value update.

    Lifecycle wording alone must not create a new task or overwrite a useful
    task description.  Explicit values (for example a new supplier) are
    different: keeping the canonical task but refreshing its current value is
    the intended V3 behavior.
    """
    # ``object_value`` is a display/extraction fallback for a task's material;
    # it changes with wording such as “需要完成 X” -> “已经完成 X”.  It is not
    # evidence of a supplier/value replacement.  Only explicit slots may
    # bypass the lifecycle state machine.
    candidate_values = {
        "old_value": str(candidate.scope.get("old_value") or "").strip(),
        "new_value": str(candidate.scope.get("new_value") or "").strip(),
    }
    memory_values = {
        "old_value": _scope(memory, "old_value"),
        "new_value": _scope(memory, "new_value"),
    }
    return any(
        candidate_value
        and normalize_content(candidate_value) != normalize_content(memory_values[name])
        for name, candidate_value in candidate_values.items()
    )


_EXPLICIT_REOPEN_MARKERS = ("重新", "再次", "重做", "返工", "恢复", "再开始", "又开始")


def _implicit_terminal_reactivation(candidate: MemoryCandidate, memory: MemoryRecord) -> bool:
    """Require explicit wording before reopening a completed/cancelled task.

    “已经完成 X” followed by “正在做 X” is contradictory unless the user
    explicitly says the task was reopened.  Treating every such pair as a
    valid transition hides the conflict from ``/memory pending``.
    """
    if memory.task_status not in {"done", "cancelled"}:
        return False
    if candidate.task_status not in {"todo", "in_progress"}:
        return False
    text = f"{candidate.evidence_span or ''} {candidate.content}"
    return not any(marker in text for marker in _EXPLICIT_REOPEN_MARKERS)


def _task_refines_existing_identity(candidate: MemoryCandidate, memory: MemoryRecord) -> bool:
    """Allow a later, more specific name for the same active task.

    A user often starts with a short task name ("做简历") and only later
    supplies the qualifier ("做 Agent 开发简历").  Exact keys remain the normal
    path.  This narrow suffix refinement prevents a false split without
    treating merely similar task text as permission to merge.
    """
    if not _same(candidate.subject, memory.subject):
        return False
    if not _same(str(candidate.scope.get("operation") or ""), _scope(memory, "operation")):
        return False
    if not _same(str(candidate.scope.get("scope") or "global"), _scope(memory, "scope", "global")):
        return False
    current = normalize_content(memory.predicate or "")
    refined = normalize_content(candidate.predicate or "")
    if len(current) < 2 or len(refined) <= len(current) or not refined.endswith(current):
        return False
    return task_policy.can_transition(memory.task_status, candidate.task_status) or candidate.task_status == memory.task_status


def evaluate_relation(candidate: MemoryCandidate, memory: MemoryRecord) -> RelationGuardResult:
    """Classify one candidate without treating retrieval similarity as authority."""
    if candidate.memory_type != memory.memory_type:
        return RelationGuardResult("new", "insert", "memory_type_mismatch", False)

    exact_key = candidate.effective_memory_key == memory.effective_memory_key
    if candidate.memory_type == "task":
        if not exact_key:
            if task_identity_compatible(candidate, memory):
                if _task_values_changed(candidate, memory):
                    return RelationGuardResult("update_task", "update_task", "legacy_task_identity_value_update", True)
                if candidate.task_status == memory.task_status:
                    return RelationGuardResult("same", "add_source", "legacy_task_identity_and_status", True)
                if _implicit_terminal_reactivation(candidate, memory):
                    return RelationGuardResult("conflict", "pending_review", "terminal_task_reactivation_requires_explicit_wording", False)
                if task_policy.can_transition(memory.task_status, candidate.task_status):
                    return RelationGuardResult("update_task", "update_task", "legacy_task_identity_valid_state_transition", True)
                return RelationGuardResult("conflict", "pending_review", "legacy_task_identity_invalid_state_transition", False)
            if _task_refines_existing_identity(candidate, memory):
                return RelationGuardResult("update_task", "update_task", "task_identity_refined_by_specific_suffix", True)
            return RelationGuardResult("new", "insert", "task_requires_exact_canonical_key", False)
        if _task_values_changed(candidate, memory):
            return RelationGuardResult("update_task", "update_task", "exact_task_key_explicit_value_update", True)
        if candidate.task_status == memory.task_status:
            return RelationGuardResult("same", "add_source", "same_task_identity_and_status", True)
        if _implicit_terminal_reactivation(candidate, memory):
            return RelationGuardResult("conflict", "pending_review", "terminal_task_reactivation_requires_explicit_wording", False)
        if task_policy.can_transition(memory.task_status, candidate.task_status):
            return RelationGuardResult("update_task", "update_task", "exact_task_key_valid_state_transition", True)
        return RelationGuardResult("conflict", "pending_review", "exact_task_key_invalid_state_transition", False)

    if candidate.memory_type == "semantic":
        # The old generic (用户, fact) representation is retrieval context only;
        # it must never be interpreted as an identity match.
        generic_fact = normalize_content(candidate.predicate or "") in {"fact", "事实"} or normalize_content(memory.predicate or "") in {"fact", "事实"}
        if generic_fact and not exact_key:
            return RelationGuardResult("new", "insert", "generic_semantic_fact_cannot_auto_merge", False)
        if not exact_key:
            return RelationGuardResult("new", "insert", "semantic_requires_exact_stable_slot_key", False)
        if candidate.normalized_content == memory.normalized_content:
            return RelationGuardResult("same", "add_source", "same_semantic_key_and_content", True)
        if _same(candidate.subject, memory.subject) and _same(candidate.predicate, memory.predicate):
            return RelationGuardResult("conflict", "pending_review", "stable_semantic_slot_changed_requires_review", False)
        return RelationGuardResult("new", "insert", "semantic_identity_not_confirmed", False)

    if candidate.memory_type == "preference":
        same_scope = normalize_content(str(candidate.scope.get("scope") or "global")) == normalize_content(_scope(memory, "scope", "global"))
        if exact_key and _same(candidate.subject, memory.subject) and same_scope:
            if preference_policy.is_ambiguous_conflict(candidate.content, memory.content):
                return RelationGuardResult("conflict", "pending_review", "ambiguous_preference_conflict", False)
            if preference_policy.explicitly_replaces(candidate.content, memory.content):
                return RelationGuardResult("supersede", "supersede", "explicit_preference_change", True)
            return RelationGuardResult("same", "add_source", "same_preference_identity", True)
        return RelationGuardResult("new", "insert", "preference_identity_mismatch", False)

    if exact_key and candidate.normalized_content == memory.normalized_content:
        return RelationGuardResult("same", "add_source", "same_episodic_key_and_content", True)
    return RelationGuardResult("new", "insert", "episodic_requires_exact_duplicate", False)
