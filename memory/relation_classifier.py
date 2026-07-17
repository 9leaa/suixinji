"""Compatibility adapter for the original Memory V2 relation API."""

from __future__ import annotations

from dataclasses import dataclass

from memory.adjudicator import adjudicate_memory
from memory.models import MemoryCandidate, MemoryRecord


LEGACY_RELATIONS = {
    "new": "new",
    "same": "same",
    "merge": "extend",
    "update_task": "update",
    "supersede": "update",
    "conflict": "contradict",
}


@dataclass(frozen=True)
class RelationDecision:
    relation: str
    action: str
    target_memory_id: str | None = None
    reason: str | None = None


def classify_relation(candidate: MemoryCandidate, memories: list[MemoryRecord]) -> RelationDecision:
    """Map the formal adjudication result to the legacy caller contract."""
    decision = adjudicate_memory(candidate, memories)
    return RelationDecision(
        relation=LEGACY_RELATIONS[decision.relation],
        action=decision.recommended_action,
        target_memory_id=decision.target_memory_ids[0] if decision.target_memory_ids else None,
        reason=decision.reason,
    )
