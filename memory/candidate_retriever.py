"""文件作用：候选相关记忆召回。

项目关系：本文件依赖 `core.settings`、`memory.canonicalizer`、`memory.models`、`memory.policies` 等 6 个模块；被 `memory.adjudicator`、`memory.consolidator`。
"""



from __future__ import annotations

from core.settings import MEMORY_ADJUDICATION_TOP_K, MEMORY_RETRIEVAL_MODE
from memory.canonicalizer import task_family_compatible, task_instance_authorized
from memory.models import MemoryCandidate, MemoryRecord, normalize_content
from memory.policies import preference as preference_policy
from memory.policies import task as task_policy
from memory.relation_guard import is_v3_candidate
from memory.repository import hybrid_adjudication_candidates, list_adjudication_candidates


def _char_similarity(left: str, right: str) -> float:
    """函数功能：`_char_similarity` 负责处理 char similarity，服务于本文件职责：候选相关记忆召回。
    传参：
        left: left 参数，由调用方传入，类型为 `str`。
        right: right 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `float`，表示计算得到的数值结果。
    """
    left_set = set(normalize_content(left))
    right_set = set(normalize_content(right))
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def task_family_similarity(candidate: MemoryCandidate, memory: MemoryRecord) -> float:
    """Broad recall score; this score alone never authorizes a state mutation."""
    if candidate.memory_type != "task" or memory.memory_type != "task":
        return 0.0
    if not task_policy.identifiers_compatible(candidate.content, memory.content):
        return 0.0
    candidate_scope = normalize_content(str(candidate.scope.get("scope") or "global"))
    memory_scope = normalize_content(str(memory.scope.get("scope") or "global"))
    if candidate_scope != memory_scope:
        return 0.0
    candidate_topic = str(candidate.predicate or candidate.scope.get("canonical_topic") or candidate.content)
    memory_topic = str(memory.predicate or memory.scope.get("canonical_topic") or memory.content)
    topic_score = _char_similarity(candidate_topic, memory_topic)
    content_score = _char_similarity(candidate.content, memory.content)
    entity_bonus = 0.18 if (
        candidate.subject and memory.subject
        and normalize_content(candidate.subject) == normalize_content(memory.subject)
    ) else 0.0
    score = 0.58 * topic_score + 0.24 * content_score + entity_bonus
    # This is a bounded recall-only fallback. Exact family membership and
    # strict task-instance compatibility have their own higher tiers below.
    return round(min(score, 0.60), 4) if score >= 0.28 else 0.0


def preference_family_similarity(candidate: MemoryCandidate, memory: MemoryRecord) -> float:
    """Family-level preference recall, with named anchors kept hard."""
    if candidate.memory_type != "preference" or memory.memory_type != "preference":
        return 0.0
    left = preference_policy.preference_signature(candidate.content, candidate.object_value)
    right = preference_policy.preference_signature(memory.content, memory.object_value)
    if left.named_anchors or right.named_anchors:
        return 0.0 if set(left.named_anchors) != set(right.named_anchors) else 0.82
    if left.scopes and right.scopes and not set(left.scopes) & set(right.scopes):
        return 0.0
    score = _char_similarity(left.normalized_topic, right.normalized_topic)
    return round(min(0.78, 0.45 + score * 0.33), 4) if score >= 0.22 else 0.0


def candidate_similarity(candidate: MemoryCandidate, memory: MemoryRecord) -> float:
    """函数功能：`candidate_similarity` 负责处理 candidate similarity，服务于本文件职责：候选相关记忆召回。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        memory: memory 参数，由调用方传入，类型为 `MemoryRecord`。
    返回结果说明：
        返回 `float`，表示计算得到的数值结果。
    """

    #精确key，优先级最高
    exact_key = candidate.effective_memory_key == memory.effective_memory_key
    if exact_key:
        return 1.0
    if candidate.normalized_content == memory.normalized_content:
        return 0.98

    # Family/assertion keys are structured extractor output, not logical test
    # references.  They provide bounded recall for incomplete legacy keys;
    # the adjudicator still decides whether a mutation is allowed.
    if candidate.memory_type == memory.memory_type == "preference":
        candidate_assertion = str(candidate.scope.get("preference_assertion_key") or "")
        memory_assertion = str(memory.scope.get("preference_assertion_key") or "")
        if candidate_assertion and candidate_assertion == memory_assertion:
            return 0.96
        candidate_family = str(candidate.scope.get("preference_family_key") or "")
        memory_family = str(memory.scope.get("preference_family_key") or "")
        if candidate_family and candidate_family == memory_family:
            return 0.84
    if candidate.memory_type == memory.memory_type == "task":
        candidate_family = str(candidate.scope.get("task_family_key") or "")
        memory_family = str(memory.scope.get("task_family_key") or "")
        if task_instance_authorized(candidate, memory):
            return 0.90
        if candidate_family and candidate_family == memory_family:
            # Family recall is deliberately broader than identity mutation.
            # A concrete instance key (round1 vs round2) must not hide the
            # related task from adjudication; RelationGuard decides whether it
            # is a new instance, merge, or pending review.
            return 0.75

    if is_v3_candidate(candidate):
        # V3 检索仍可能返回宽泛 semantic 候选，但是否有用由 identity 槽位决定，而不是共享用户/模板决定。
        if candidate.memory_type != memory.memory_type:
            return 0.0
        if candidate.memory_type == "task":
            if task_family_compatible(candidate, memory):
                return 0.68
            # 任务的常规身份是精确 canonical key；唯一有意保留的例外是后续标题严格细化早期短标题。
            # 这里只把候选放入小规模裁决集；Relation Guard 仍是唯一允许批准变更的组件。
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
                return 0.66
            return task_family_similarity(candidate, memory)
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
            comparative_alternative = preference_policy.is_comparative_alternative(candidate.content, memory.content)
            ambiguous_conflict = preference_policy.is_ambiguous_conflict(candidate.content, memory.content)
            if preference_policy.topic_compatibility(candidate, memory) < 0.75 and not comparative_alternative and not ambiguous_conflict:
                return preference_family_similarity(candidate, memory)

    # 检索发生在裁决前，并被限制为较小 top-k；不能让共享句式模板或 A1/A10 这类子串关系把真正同主题 Memory 挤出列表。
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
    """函数功能：`retrieval_signals` 负责处理 retrieval signals，服务于本文件职责：候选相关记忆召回。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        memory: memory 参数，由调用方传入，类型为 `MemoryRecord`。
    返回结果说明：
        返回 `dict[str, object]`，表示结构化结果、载荷或状态映射。
    """
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
    """函数功能：`retrieve_candidates` 负责处理 retrieve candidates，服务于本文件职责：候选相关记忆召回。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[MemoryRecord]`，表示按条件筛选、构造或查询得到的列表。
    """
    top_k = limit if limit is not None else MEMORY_ADJUDICATION_TOP_K
    if MEMORY_RETRIEVAL_MODE == "hybrid":
        # Multi-channel retrieval is intentionally over-fetched before this
        # module applies its type-aware deterministic identity ranking. This
        # preserves an exact/family anchor when a broad lexical channel has
        # many weak hits; only the final bounded Top-K reaches adjudication.
        memories = hybrid_adjudication_candidates(space_id, candidate, limit=max(top_k * 6, 50))
    else:
        memories = list_adjudication_candidates(
            space_id,
            memory_type=candidate.memory_type,
            memory_key=candidate.effective_memory_key,
            limit=200,
        )
    # Semantic writes only need duplicate detection.  Broad-facet neighbours
    # are intentionally not adjudication candidates because semantic facts
    # are append-only and cannot update one another.
    if candidate.memory_type == "semantic":
        return [
            memory for memory in memories
            if candidate.effective_memory_key == memory.effective_memory_key
            or candidate.normalized_content == memory.normalized_content
        ][:1]
    scored = [(memory, candidate_similarity(candidate, memory)) for memory in memories]
    scored.sort(key=lambda item: (item[1], item[0].updated_at), reverse=True)
    return [memory for memory, score in scored[: max(1, min(int(top_k), 20))] if score >= 0.18]
