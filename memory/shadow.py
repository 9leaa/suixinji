"""文件作用：V3 shadow 对比。

项目关系：本文件依赖 `core`、`memory.canonicalizer`、`memory.models`、`memory.relation_guard`；被 `memory.consolidator`、`memory.service`。
"""



from __future__ import annotations

from typing import Any

from core import settings
from memory.canonicalizer import canonicalize_candidate
from memory.models import MemoryCandidate, MemoryDecision, MemoryRecord
from memory.relation_guard import evaluate_relation


def build_shadow_report(candidates: list[MemoryCandidate]) -> dict[str, Any] | None:
    """函数功能：`build_shadow_report` 负责构建 shadow report，服务于本文件职责：V3 shadow 对比。
    传参：
        candidates: candidates 参数，由调用方传入，类型为 `list[MemoryCandidate]`。
    返回结果说明：
        返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
    """
    if not settings.MEMORY_V3_SHADOW_MODE:
        return None

    rows: list[dict[str, str | bool]] = []
    changed_identity_count = 0
    for candidate in candidates[:8]:
        projected = canonicalize_candidate(candidate)
        changed = projected.effective_memory_key != candidate.effective_memory_key
        changed_identity_count += int(changed)
        rows.append(
            {
                "memory_type": candidate.memory_type,
                "legacy_key": candidate.effective_memory_key[:180],
                "v3_key": projected.effective_memory_key[:180],
                "key_changed": changed,
            }
        )
    return {
        "candidate_count": len(candidates),
        "changed_identity_count": changed_identity_count,
        "candidates": rows,
        "write_path": "unchanged",
    }


def build_relation_shadow_report(
    candidate: MemoryCandidate,
    memories: list[MemoryRecord],
    actual: MemoryDecision,
) -> dict[str, Any] | None:
    """函数功能：`build_relation_shadow_report` 负责构建 relation shadow report，服务于本文件职责：V3 shadow 对比。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        memories: memories 参数，由调用方传入，类型为 `list[MemoryRecord]`。
        actual: actual 参数，由调用方传入，类型为 `MemoryDecision`。
    返回结果说明：
        返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
    """
    if not settings.MEMORY_V3_SHADOW_MODE:
        return None
    projected = canonicalize_candidate(candidate)
    exact = [memory for memory in memories if memory.effective_memory_key == projected.effective_memory_key]
    if exact:
        guarded = evaluate_relation(projected, max(exact, key=lambda item: (item.updated_at, item.current_version)))
        projected_relation = guarded.relation
        projected_action = guarded.action
        reason = guarded.reason
    else:
        projected_relation = "new"
        projected_action = "insert"
        reason = "v3_no_exact_canonical_identity"
    return {
        "memory_type": candidate.memory_type,
        "actual_relation": actual.relation,
        "actual_action": actual.recommended_action,
        "v3_relation": projected_relation,
        "v3_action": projected_action,
        "decision_changed": (actual.relation, actual.recommended_action) != (projected_relation, projected_action),
        "reason": reason,
        "write_path": "unchanged",
    }
