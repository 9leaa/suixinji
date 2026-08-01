"""文件作用：关系安全门。

项目关系：本文件依赖 `memory.canonicalizer`、`memory.models`、`memory.policies`；被 `memory.adjudicator`、`memory.candidate_retriever`、`memory.shadow`。
"""



from __future__ import annotations

from dataclasses import dataclass

from memory.canonicalizer import task_identity_compatible
from memory.models import MEMORY_KEY_V3_VERSION, MemoryCandidate, MemoryRecord, normalize_content
from memory.policies import preference as preference_policy
from memory.policies import task as task_policy


@dataclass(frozen=True)
class RelationGuardResult:
    """类功能：`RelationGuardResult` 封装与“关系安全门”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    relation: str
    action: str
    reason: str
    approved: bool


def _same(left: str | None, right: str | None) -> bool:
    """函数功能：`_same` 负责处理 same，服务于本文件职责：关系安全门。
    传参：
        left: left 参数，由调用方传入，类型为 `str | None`。
        right: right 参数，由调用方传入，类型为 `str | None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    return bool(left and right and normalize_content(left) == normalize_content(right))


def _scope(memory: MemoryRecord, key: str, default: str = "") -> str:
    """函数功能：`_scope` 负责处理 scope，服务于本文件职责：关系安全门。
    传参：
        memory: memory 参数，由调用方传入，类型为 `MemoryRecord`。
        key: key 参数，由调用方传入，类型为 `str`。
        default: default 参数，由调用方传入，类型为 `str`，默认值为 `''`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return str(memory.scope.get(key) or default)


def is_v3_candidate(candidate: MemoryCandidate) -> bool:
    """函数功能：`is_v3_candidate` 负责判断是否为 v3 candidate，服务于本文件职责：关系安全门。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    return candidate.memory_key_version == MEMORY_KEY_V3_VERSION or candidate.scope.get("memory_key_version") == MEMORY_KEY_V3_VERSION


def _task_values_changed(candidate: MemoryCandidate, memory: MemoryRecord) -> bool:
    """函数功能：`_task_values_changed` 负责处理 task values changed，服务于本文件职责：关系安全门。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        memory: memory 参数，由调用方传入，类型为 `MemoryRecord`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    # `object_value` 只是任务材料的展示/抽取 fallback，会随“需要完成 X”到“已经完成 X”这类表述变化。
    # 它不是供应方或取值替换证据；只有显式槽位才允许绕过生命周期状态机。
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
    """函数功能：`_implicit_terminal_reactivation` 负责处理 implicit terminal reactivation，服务于本文件职责：关系安全门。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        memory: memory 参数，由调用方传入，类型为 `MemoryRecord`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    if memory.task_status not in {"done", "cancelled"}:
        return False
    if candidate.task_status not in {"todo", "in_progress"}:
        return False
    text = f"{candidate.evidence_span or ''} {candidate.content}"
    return not any(marker in text for marker in _EXPLICIT_REOPEN_MARKERS)


def _task_refines_existing_identity(candidate: MemoryCandidate, memory: MemoryRecord) -> bool:
    """函数功能：`_task_refines_existing_identity` 负责处理 task refines existing identity，服务于本文件职责：关系安全门。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        memory: memory 参数，由调用方传入，类型为 `MemoryRecord`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
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
    """函数功能：`evaluate_relation` 负责处理 evaluate relation，服务于本文件职责：关系安全门。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        memory: memory 参数，由调用方传入，类型为 `MemoryRecord`。
    返回结果说明：
        返回 `RelationGuardResult` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
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
        # 旧版泛化 `(用户, fact)` 表示只能作为检索语境，绝不能被解释为身份匹配。
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
