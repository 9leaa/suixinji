"""Deterministically evolve memory state from an adjudicated decision."""

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
    """Apply an action through the repository's atomic evolution transaction."""
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
    }
    add_step(
        trace,
        step,
        output_summary={key: value for key, value in output.items() if value is not None},
        duration_ms=int((time.perf_counter() - evolution_started) * 1000),
        reason=decision.reason,
    )
    return result
