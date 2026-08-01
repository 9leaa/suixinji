"""文件作用：最终裁决。

项目关系：本文件依赖 `core`、`core.settings`、`memory.candidate_retriever`、`memory.models` 等 6 个模块；被 `eval.eval_memory`、`eval.eval_memory_quality`、`memory.consolidator`、`memory.relation_classifier` 等 6 个模块。
"""



from __future__ import annotations

import hashlib
import re

from core import settings
from core.settings import MEMORY_AUTO_MUTATION_MIN_CONFIDENCE
from memory.candidate_retriever import candidate_similarity
from memory.models import MemoryCandidate, MemoryDecision, MemoryRecord
from memory.policies import preference as preference_policy
from memory.policies import semantic as semantic_policy
from memory.policies import task as task_policy
from memory.relation_guard import evaluate_relation, is_v3_candidate


DESTRUCTIVE_ACTIONS = {"merge", "update_task", "supersede", "conflict"}


def _combined_confidence(candidate: MemoryCandidate, relation_confidence: float) -> float:
    """函数功能：`_combined_confidence` 负责处理 combined confidence，服务于本文件职责：最终裁决。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        relation_confidence: relation confidence 参数，由调用方传入，类型为 `float`。
    返回结果说明：
        返回 `float`，表示计算得到的数值结果。
    """
    return 0.6 * float(candidate.confidence) + 0.4 * relation_confidence


def _safe_evidence(candidate: MemoryCandidate, memories: list[MemoryRecord]) -> list[str]:
    """函数功能：`_safe_evidence` 负责处理 safe evidence，服务于本文件职责：最终裁决。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        memories: memories 参数，由调用方传入，类型为 `list[MemoryRecord]`。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
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
    """函数功能：`_decision` 负责处理 decision，服务于本文件职责：最终裁决。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        relation: relation 参数，由调用方传入，类型为 `str`。
        action: action 参数，由调用方传入，类型为 `str`。
        confidence: confidence 参数，由调用方传入，类型为 `float`。
        reason: reason 参数，由调用方传入，类型为 `str`。
        targets: targets 参数，由调用方传入，类型为 `list[MemoryRecord] | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryDecision` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
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
    """函数功能：`_shares_topic` 负责处理 shares topic，服务于本文件职责：最终裁决。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        memory: memory 参数，由调用方传入，类型为 `MemoryRecord`。
        similarity: similarity 参数，由调用方传入，类型为 `float`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
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
    """函数功能：`_near_same` 负责处理 near same，服务于本文件职责：最终裁决。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        memory: memory 参数，由调用方传入，类型为 `MemoryRecord`。
        similarity: similarity 参数，由调用方传入，类型为 `float`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
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
            #Preference：极性、范围、限定词必须相同；
            return (
                candidate_signature.polarity == memory_signature.polarity
                and candidate_signature.qualifiers == memory_signature.qualifiers
                and candidate_signature.scopes == memory_signature.scopes
            )
        if candidate.memory_type == "task":
            #任务状态必须一致
            return candidate.task_status == memory.task_status
        if candidate.memory_type == "semantic" and _semantic_conflict(candidate, memory):
            return False
        if candidate.memory_type == "semantic" and candidate.object_value and memory.object_value:
            return candidate.object_value.casefold() == memory.object_value.casefold()
        return True
    shorter, longer = sorted((left, right), key=len)
    return shorter in longer and len(shorter) / len(longer) >= 0.9


def _shares_named_token(candidate: MemoryCandidate, memory: MemoryRecord) -> bool:
    """函数功能：`_shares_named_token` 负责处理 shares named token，服务于本文件职责：最终裁决。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        memory: memory 参数，由调用方传入，类型为 `MemoryRecord`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    candidate_tokens = {token.casefold() for token in re.findall(r"[A-Za-z][A-Za-z0-9+#.-]*", candidate.content)}
    memory_tokens = {token.casefold() for token in re.findall(r"[A-Za-z][A-Za-z0-9+#.-]*", memory.content)}
    return bool(candidate_tokens & memory_tokens)


def _semantic_conflict(candidate: MemoryCandidate, memory: MemoryRecord) -> bool:
    """函数功能：`_semantic_conflict` 负责处理 semantic conflict，服务于本文件职责：最终裁决。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        memory: memory 参数，由调用方传入，类型为 `MemoryRecord`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    if candidate.predicate == memory.predicate == "location":
        return not semantic_policy.explicitly_replaces(candidate.content, predicate="location")
    negative_markers = ("没有", "不是", "并非", "无问题", "不存在")
    candidate_negative = any(marker in candidate.content for marker in negative_markers)
    memory_negative = any(marker in memory.content for marker in negative_markers)
    return candidate_negative != memory_negative


def _adjudicate_v3(candidate: MemoryCandidate, memories: list[MemoryRecord]) -> MemoryDecision:
    """函数功能：`_adjudicate_v3` 负责处理 adjudicate v3，服务于本文件职责：最终裁决。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        memories: memories 参数，由调用方传入，类型为 `list[MemoryRecord]`。
    返回结果说明：
        返回 `MemoryDecision` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    exact = [memory for memory in memories if candidate.effective_memory_key == memory.effective_memory_key]
    if candidate.memory_type == "preference":
        conflicts = [
            memory for memory in memories
            if preference_policy.scopes_compatible(candidate, memory)
            and (
                preference_policy.is_ambiguous_conflict(candidate.content, memory.content)
                or preference_policy.is_comparative_alternative(candidate.content, memory.content)
            )
        ]
        if conflicts:
            best = max(conflicts, key=lambda memory: (memory.updated_at, memory.current_version))
            return _decision(candidate, "conflict", "pending_review", _combined_confidence(candidate, 0.82), "ambiguous_preference_conflict", [best])

    if not exact:
        # 默认要求精确身份匹配；guard 只允许一个窄例外：active 短任务名可被后续更具体标题细化。
        # 检索只负责提供候选，是否允许该变更仍以 Relation Guard 为准。
        refinements = [
            memory
            for memory in memories
            if (guarded := evaluate_relation(candidate, memory)).approved and guarded.action == "update_task"
        ]
        if not refinements:
            return _decision(candidate, "new", "insert", max(0.8, candidate.confidence), "v3_no_exact_canonical_identity")
        best = max(refinements, key=lambda memory: (candidate_similarity(candidate, memory), memory.updated_at, memory.current_version))
        guarded = evaluate_relation(candidate, best)
        confidence = _combined_confidence(candidate, max(0.82, candidate_similarity(candidate, best)))
        return _decision(candidate, guarded.relation, guarded.action, confidence, guarded.reason, [best])

    best = max(exact, key=lambda memory: (memory.updated_at, memory.current_version))
    guarded = evaluate_relation(candidate, best)
    similarity = candidate_similarity(candidate, best)
    if guarded.action == "update_task":
        confidence = _combined_confidence(candidate, max(0.82, similarity))
    elif guarded.action in {"supersede", "pending_review"}:
        confidence = _combined_confidence(candidate, max(0.8, similarity))
    else:
        confidence = max(candidate.confidence, 0.92 if guarded.action == "add_source" else 0.8)
    return _decision(candidate, guarded.relation, guarded.action, confidence, guarded.reason, [best])


def adjudicate_memory(candidate: MemoryCandidate, memories: list[MemoryRecord]) -> MemoryDecision:
    """函数功能：`adjudicate_memory` 负责处理 adjudicate memory，服务于本文件职责：最终裁决。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        memories: memories 参数，由调用方传入，类型为 `list[MemoryRecord]`。
    返回结果说明：
        返回 `MemoryDecision` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    if not candidate.should_store:
        return _decision(candidate, "new", "discard", candidate.confidence, candidate.effective_reason or "candidate_should_not_store")
    if not memories:
        return _decision(candidate, "new", "insert", max(0.8, candidate.confidence), "no_related_active_memory")

    if settings.MEMORY_RELATION_GUARD_V3_ENABLED and is_v3_candidate(candidate):
        return _adjudicate_v3(candidate, memories)

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
