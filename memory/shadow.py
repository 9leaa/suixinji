"""Read-only Memory V3 shadow signals used during staged rollout."""

from __future__ import annotations

from typing import Any

from core import settings
from memory.canonicalizer import canonicalize_candidate
from memory.models import MemoryCandidate, MemoryDecision, MemoryRecord
from memory.relation_guard import evaluate_relation


def build_shadow_report(candidates: list[MemoryCandidate]) -> dict[str, Any] | None:
    """Compare legacy candidates with their V3 identities without changing writes.

    The report intentionally includes only type/key-level metadata.  It is
    suitable for durable traces and never stores note text or model output.
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
    """Project a V3 relation decision without changing retrieval or writes."""
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
