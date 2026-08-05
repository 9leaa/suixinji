"""文件作用：决策落库后的确定性演化。

项目关系：本文件依赖 `memory.models`、`memory.policies`、`memory.repository`、`memory.trace`；被 `memory.consolidator`、`tests.test_memory_adjudication_evolution`。
"""



from __future__ import annotations

import time
from typing import Any

from memory.models import MemoryCandidate, MemoryDecision
from memory.policies import merge_content
from memory.repository import apply_memory_decision, get_memory
from memory.trace import add_step


TRACE_STEPS = {
    "insert": "memory_inserted",
    "add_source": "memory_source_added",
    "merge": "memory_merged",
    "update": "memory_updated",
    "update_task": "memory_updated",
    "supersede": "memory_superseded",
    "conflict": "memory_conflicted",
    "pending_review": "memory_pending_review",
    "discard": "memory_discarded",
}


def evolve_memory(
    *,
    space_id: str,
    note_id: str,
    candidate: MemoryCandidate,
    decision: MemoryDecision,
    trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """函数功能：`evolve_memory` 负责处理 evolve memory，服务于本文件职责：决策落库后的确定性演化。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        decision: decision 参数，由调用方传入，类型为 `MemoryDecision`。
        trace: trace 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    merged_content = None
    if decision.recommended_action == "merge" and decision.target_memory_ids:
        target = get_memory(decision.target_memory_ids[0])
        if target is None:
            raise ValueError(f"merge target not found: {decision.target_memory_ids[0]}")
        merged_content = merge_content(candidate.memory_type, target.content, candidate.content)

    add_step(
        trace,
        "evolution_started",
        input_summary={
            "candidate_id": candidate.candidate_id,
            "decision_id": decision.decision_id,
            "action": decision.recommended_action,
            "target_memory_ids": decision.target_memory_ids,
        },
        reason=decision.reason,
    )
    evolution_started = time.perf_counter()
    result = apply_memory_decision(
        space_id,
        note_id,
        candidate,
        decision,
        merged_content=merged_content,
    )
    step = TRACE_STEPS.get(decision.recommended_action, "memory_evolved")
    output = {
        "candidate_id": candidate.candidate_id,
        "decision_id": decision.decision_id,
        "memory_id": result.get("memory_id"),
        "target_memory_id": result.get("target_memory_id"),
        "action": result.get("action"),
        "relation": decision.relation,
        "archived_duplicate_ids": result.get("archived_duplicate_ids"),
    }
    add_step(
        trace,
        step,
        output_summary={key: value for key, value in output.items() if value is not None},
        duration_ms=int((time.perf_counter() - evolution_started) * 1000),
        reason=decision.reason,
    )
    return result
