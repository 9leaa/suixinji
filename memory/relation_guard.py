"""文件作用：关系安全门。

项目关系：本文件依赖 `memory.canonicalizer`、`memory.models`、`memory.policies`；被 `memory.adjudicator`、`memory.candidate_retriever`、`memory.shadow`。
"""



from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

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
_IMPLICIT_REOPEN_MARKERS = ("还需要", "仍需", "继续", "尚未", "未完成", "待完善", "没做完", "接下来要")
_CORRECTION_MARKERS = ("不准确", "错误记录", "说错", "更正", "实际是", "正确的是")
_UNCONFIRMED_MARKERS = ("另一条", "未确认", "没有说明", "据说", "听说")
_DETAIL_MARKERS = ("并补充", "补充了", "详细", "详情", "会后记录", "负责人", "截止", "长期状态", "工作时", "工作日", "早上", "下午", "晚上")


def _terminal_reactivation_requires_review(candidate: MemoryCandidate, memory: MemoryRecord) -> bool:
    if memory.task_status != "done" or candidate.task_status != "todo":
        return False
    text = f"{candidate.evidence_span or ''} {candidate.content}"
    return not any(marker in text for marker in (*_EXPLICIT_REOPEN_MARKERS, *_IMPLICIT_REOPEN_MARKERS))


def _candidate_scope(candidate: MemoryCandidate, key: str) -> str:
    return str(candidate.scope.get(key) or "").strip()


def _memory_scope(memory: MemoryRecord, key: str) -> str:
    return str(memory.scope.get(key) or "").strip()


def _same_structured_value(candidate: MemoryCandidate, memory: MemoryRecord) -> bool:
    candidate_value = _candidate_scope(candidate, "new_value") or str(candidate.object_value or "")
    memory_value = _memory_scope(memory, "new_value") or str(memory.object_value or "")
    return _same(candidate_value, memory_value)


def _has_detail_delta(candidate: MemoryCandidate, memory: MemoryRecord) -> bool:
    if candidate.normalized_content == memory.normalized_content:
        return False
    text = f"{candidate.evidence_span or ''} {candidate.content}"
    return any(marker in text for marker in _DETAIL_MARKERS)


def _task_metadata_changed(candidate: MemoryCandidate, memory: MemoryRecord) -> bool:
    """Return whether task lifecycle metadata changed while identity/status did not.

    A blocker or closure update is still a new task version even when the
    binary task status remains ``todo``. Progress paraphrases may remain a
    supporting source, while blocker/closure deltas must not be lost.
    """
    # A concise, separately extracted progress assertion is a lifecycle
    # refinement and must be versioned.  The ordinary extractor often stores
    # the entire task sentence as ``progress_note``; treating that wording as
    # a new version would turn a harmless paraphrase into duplicate versions.
    candidate_progress = normalize_content(str(candidate.scope.get("progress_note") or ""))
    candidate_content = normalize_content(candidate.content)
    memory_progress = normalize_content(str(memory.scope.get("progress_note") or ""))
    if candidate_progress and candidate_progress != memory_progress and candidate_progress not in candidate_content:
        return True
    # Blocker/closure changes alter the operational state and must create a
    # new version.
    for key in ("blocker", "closure_reason"):
        left = normalize_content(str(candidate.scope.get(key) or ""))
        right = normalize_content(str(memory.scope.get(key) or ""))
        if left != right and (left or right):
            return True
    return False


def _is_correction(candidate: MemoryCandidate) -> bool:
    text = f"{candidate.evidence_span or ''} {candidate.content}"
    return any(marker in text for marker in _CORRECTION_MARKERS)


def _is_unconfirmed(candidate: MemoryCandidate) -> bool:
    text = f"{candidate.evidence_span or ''} {candidate.content}"
    return any(marker in text for marker in _UNCONFIRMED_MARKERS)


def _is_stale_candidate(candidate: MemoryCandidate, memory: MemoryRecord) -> bool:
    observed = _candidate_scope(candidate, "observed_at")
    if not observed:
        return False
    # `updated_at` is persistence time and is not an evidence timestamp.  A
    # pair of concurrent candidates can legitimately share the same
    # `observed_at` while the first transaction has already changed
    # `updated_at`.  Prefer the last evidence timestamp stored in the
    # structured scope; fall back to `updated_at` only for legacy/seed rows
    # that have no evidence timestamp yet.
    previous_observed = _memory_scope(memory, "observed_at") or str(memory.updated_at or "").strip()
    if not previous_observed:
        return False
    try:
        candidate_time = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        previous_time = datetime.fromisoformat(previous_observed.replace("Z", "+00:00"))
        return candidate_time < previous_time
    except ValueError:
        # A malformed timestamp must not silently trigger a state mutation.
        return True


def _task_refines_existing_identity(candidate: MemoryCandidate, memory: MemoryRecord) -> bool:
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
    if candidate.memory_type != memory.memory_type:
        return RelationGuardResult("new", "insert", "memory_type_mismatch", False)

    exact_key = candidate.effective_memory_key == memory.effective_memory_key
    if exact_key and _is_stale_candidate(candidate, memory):
        return RelationGuardResult("conflict", "pending_review", "candidate_observed_before_current_memory", False)
    if candidate.memory_type == "task":
        compatible = exact_key or task_identity_compatible(candidate, memory) or _task_refines_existing_identity(candidate, memory)
        if not compatible:
            return RelationGuardResult("new", "insert", "task_requires_exact_canonical_key", False)
        if _task_values_changed(candidate, memory):
            return RelationGuardResult("update_task", "update_task", "task_explicit_value_update", True)
        if candidate.task_status == memory.task_status:
            candidate_closure = str(candidate.scope.get("closure_reason") or "")
            memory_closure = str(memory.scope.get("closure_reason") or "")
            if candidate.task_status == "done" and (candidate_closure or memory_closure) and candidate_closure != memory_closure:
                return RelationGuardResult("conflict", "pending_review", "task_closure_reason_conflict", False)
            if _task_metadata_changed(candidate, memory):
                return RelationGuardResult("update_task", "update_task", "task_lifecycle_metadata_update", True)
            if _has_detail_delta(candidate, memory):
                return RelationGuardResult("merge", "merge", "task_detail_merge", True)
            return RelationGuardResult("same", "add_source", "same_task_identity_and_status", True)
        if _terminal_reactivation_requires_review(candidate, memory):
            return RelationGuardResult("conflict", "pending_review", "terminal_task_reactivation_requires_evidence", False)
        if task_policy.can_transition(memory.task_status, candidate.task_status):
            return RelationGuardResult("update_task", "update_task", "valid_task_state_transition", True)
        return RelationGuardResult("conflict", "pending_review", "invalid_task_state_transition", False)

    if candidate.memory_type == "semantic":
        generic_fact = normalize_content(candidate.predicate or "") in {"fact", "事实"} or normalize_content(memory.predicate or "") in {"fact", "事实"}
        if generic_fact and not exact_key:
            return RelationGuardResult("new", "insert", "generic_semantic_fact_cannot_auto_merge", False)
        if not exact_key:
            return RelationGuardResult("new", "insert", "semantic_requires_exact_stable_slot_key", False)
        if _is_unconfirmed(candidate):
            return RelationGuardResult("conflict", "pending_review", "unconfirmed_semantic_change", False)
        if _is_correction(candidate):
            return RelationGuardResult("supersede", "update", "explicit_semantic_correction", True)
        if _same_structured_value(candidate, memory):
            if _has_detail_delta(candidate, memory):
                return RelationGuardResult("merge", "update", "semantic_detail_merge", True)
            return RelationGuardResult("same", "add_source", "same_semantic_value", True)
        if _same(candidate.subject, memory.subject) and _same(candidate.predicate, memory.predicate):
            # Coordinating language adds a compatible fact; it is not a replacement.
            if any(marker in candidate.content for marker in ("也在", "同时", "以及", "并且")):
                return RelationGuardResult("merge", "merge", "semantic_compatible_extension", True)
            return RelationGuardResult("update", "update", "semantic_slot_value_update", True)
        return RelationGuardResult("new", "insert", "semantic_identity_not_confirmed", False)

    if candidate.memory_type == "preference":
        same_scope = normalize_content(str(candidate.scope.get("scope") or "global")) == normalize_content(_scope(memory, "scope", "global"))
        if not exact_key or not _same(candidate.subject, memory.subject) or not same_scope:
            return RelationGuardResult("new", "insert", "preference_identity_mismatch", False)
        if _is_unconfirmed(candidate) or preference_policy.is_ambiguous_conflict(candidate.content, memory.content):
            return RelationGuardResult("conflict", "pending_review", "unconfirmed_preference_change", False)
        if _is_correction(candidate):
            return RelationGuardResult("supersede", "update", "explicit_preference_correction", True)
        if candidate.polarity and memory.polarity and candidate.polarity != memory.polarity:
            return RelationGuardResult("update", "update", "preference_polarity_update", True)
        if _has_detail_delta(candidate, memory):
            return RelationGuardResult("merge", "update", "preference_scope_or_detail_merge", True)
        return RelationGuardResult("same", "add_source", "same_preference_identity", True)

    if not exact_key and _is_correction(candidate) and _same(candidate.subject, memory.subject) and _same(candidate.predicate, memory.predicate):
        return RelationGuardResult("supersede", "update", "explicit_event_correction_across_topic", True)
    if exact_key:
        if _is_unconfirmed(candidate) or ("没有" in candidate.content and not _is_correction(candidate)):
            return RelationGuardResult("conflict", "pending_review", "unconfirmed_or_negative_event_claim", False)
        if _is_correction(candidate):
            return RelationGuardResult("supersede", "update", "explicit_event_correction", True)
        if _same_structured_value(candidate, memory):
            if "改为" in candidate.content or "改成" in candidate.content or "时间" in candidate.content:
                return RelationGuardResult("update", "update", "episodic_time_or_fact_update", True)
            if _has_detail_delta(candidate, memory):
                return RelationGuardResult("merge", "update", "episodic_detail_merge", True)
            return RelationGuardResult("same", "add_source", "same_episodic_event", True)
    return RelationGuardResult("new", "insert", "episodic_requires_exact_duplicate", False)
