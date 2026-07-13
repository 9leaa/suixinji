"""Rule-based relation classifier for memory candidates."""

from __future__ import annotations

from dataclasses import dataclass

from memory.models import MemoryCandidate, MemoryRecord, normalize_content

NEGATION_MARKERS = ("不", "暂时不", "不再", "不想", "不打算", "过敏", "取消")
CHANGE_MARKERS = ("改为", "搬到", "现在", "短期", "只学", "重点")
AMBIGUOUS_CONFLICT_PAIRS = (("远程", "办公室"), ("在家", "公司"))


@dataclass(frozen=True)
class RelationDecision:
    relation: str
    action: str
    target_memory_id: str | None = None
    reason: str | None = None


def _char_overlap(left: str, right: str) -> float:
    left_set = set(normalize_content(left))
    right_set = set(normalize_content(right))
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def _has_negation(text: str) -> bool:
    return any(marker in text for marker in NEGATION_MARKERS)


def _has_change(text: str) -> bool:
    return any(marker in text for marker in CHANGE_MARKERS)


def _shares_entity(candidate: MemoryCandidate, memory: MemoryRecord) -> bool:
    if any(marker in candidate.content for marker in ("搬到", "住在")) and any(marker in memory.content for marker in ("搬到", "住在")):
        return True
    if any(marker in candidate.content for marker in ("学习", "学")) and any(marker in memory.content for marker in ("学习", "学")):
        return True
    if "工作" in candidate.content and "工作" in memory.content:
        return True
    if not candidate.entities:
        return _char_overlap(candidate.content, memory.content) >= 0.35
    return any(entity and entity in memory.content for entity in candidate.entities)


def _ambiguous_conflict(left: str, right: str) -> bool:
    for first, second in AMBIGUOUS_CONFLICT_PAIRS:
        if (first in left and second in right) or (second in left and first in right):
            return True
    return False


def classify_relation(candidate: MemoryCandidate, memories: list[MemoryRecord]) -> RelationDecision:
    if not memories:
        return RelationDecision(relation="new", action="insert", reason="no_similar_memory")

    best = max(memories, key=lambda memory: _char_overlap(candidate.content, memory.content))
    overlap = _char_overlap(candidate.content, best.content)
    same_subject = candidate.memory_type == best.memory_type and _shares_entity(candidate, best)

    if same_subject and candidate.memory_type == "task" and candidate.task_status and candidate.task_status != best.task_status:
        return RelationDecision(relation="update", action="update_task", target_memory_id=best.id, reason="task_status_changed")

    if candidate.normalized_content == best.normalized_content or overlap >= 0.82:
        return RelationDecision(relation="same", action="add_source", target_memory_id=best.id, reason="same_or_near_duplicate")

    if same_subject and candidate.memory_type == "preference" and _ambiguous_conflict(candidate.content, best.content):
        return RelationDecision(relation="contradict", action="conflict", target_memory_id=best.id, reason="ambiguous_preference_conflict")

    if same_subject and (_has_negation(candidate.content) != _has_negation(best.content) or _has_change(candidate.content)):
        return RelationDecision(relation="update", action="supersede", target_memory_id=best.id, reason="same_subject_changed")

    if same_subject and overlap >= 0.45:
        return RelationDecision(relation="extend", action="merge", target_memory_id=best.id, reason="same_subject_extension")

    return RelationDecision(relation="new", action="insert", reason="no_actionable_relation")
