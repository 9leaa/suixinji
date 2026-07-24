"""Consolidate extracted candidates into versioned memories."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from core import settings
from core.settings import MEMORY_EXTRACTION_LEASE_SECONDS
from memory.adjudicator import adjudicate_memory
from memory.advisory import maybe_memory_relation_advisory
from memory.candidate_retriever import retrieve_candidates
from memory.evolution import evolve_memory
from memory.models import MemoryCandidate, utc_now_iso
from memory.repository import add_memory_relation, add_source, get_extraction_state, list_memories, mark_extraction_failed, update_memory
from memory.retriever import score_memory
from memory.trace import add_step
from storage.note_storage import is_note_queryable, load_index

LOGGER = logging.getLogger(__name__)


def _is_processing_stale(updated_at: str | None) -> bool:
    if not updated_at:
        return True
    try:
        parsed = datetime.fromisoformat(updated_at)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return (datetime.now().astimezone() - parsed).total_seconds() > MEMORY_EXTRACTION_LEASE_SECONDS


def consolidate_candidate(space_id: str, note_id: str, candidate: MemoryCandidate, *, trace: dict[str, Any] | None = None) -> dict[str, Any]:
    add_step(
        trace,
        "retrieval_started",
        input_summary={"candidate_id": candidate.candidate_id, "memory_type": candidate.memory_type},
    )
    retrieval_started = time.perf_counter()
    similar = retrieve_candidates(space_id, candidate)
    add_step(
        trace,
        "candidate_memories_found",
        input_summary={"candidate_id": candidate.candidate_id, "memory_type": candidate.memory_type},
        output_summary={"retrieved_count": len(similar), "memory_ids": [memory.id for memory in similar]},
        duration_ms=int((time.perf_counter() - retrieval_started) * 1000),
    )
    adjudication_started = time.perf_counter()
    decision = adjudicate_memory(candidate, similar)
    advisory = maybe_memory_relation_advisory(candidate, similar, decision)
    add_step(
        trace,
        "relation_decided",
        input_summary={"candidate_id": candidate.candidate_id},
        output_summary={
            "decision_id": decision.decision_id,
            "relation": decision.relation,
            "target_memory_ids": decision.target_memory_ids,
            "action": decision.recommended_action,
            "confidence": decision.confidence,
            "strong_advisory": advisory,
        },
        duration_ms=int((time.perf_counter() - adjudication_started) * 1000),
        reason=decision.reason,
    )
    return evolve_memory(
        space_id=space_id,
        note_id=note_id,
        candidate=candidate,
        decision=decision,
        trace=trace,
    )


def process_unextracted_notes(space_id: str, *, limit: int = 100) -> dict[str, Any]:
    """Daily consolidation pass: recover notes without completed extraction state."""
    from memory.service import process_note_memory

    processed = []
    failed = []
    skipped = 0
    for note in load_index(space_id)[: max(1, min(int(limit), 500))]:
        if not is_note_queryable(note):
            skipped += 1
            continue
        note_id = str(note.get("id") or "")
        if not note_id:
            skipped += 1
            continue
        state = get_extraction_state(note_id)
        if state is not None and state.status in {"completed", "empty"}:
            skipped += 1
            continue
        if state is not None and state.status == "processing":
            if not _is_processing_stale(state.updated_at):
                skipped += 1
                continue
            mark_extraction_failed(note_id, space_id, error="stale processing lease expired")
        try:
            report = process_note_memory(note)
            processed.append(
                {
                    "note_id": note_id,
                    "trace_id": report.get("trace_id"),
                    "candidates": report.get("candidates"),
                    "extraction_status": report.get("extraction_status"),
                }
            )
        except Exception as exc:
            LOGGER.exception(
                "memory.daily.note.failed space_id=%s note_id=%s error_type=%s",
                space_id,
                note_id,
                type(exc).__name__,
            )
            failed.append({"note_id": note_id, "error": f"{type(exc).__name__}: {exc}"})
    return {
        "space_id": space_id,
        "processed": processed,
        "failed": failed,
        "processed_count": len(processed),
        "failed_count": len(failed),
        "skipped_count": skipped,
        "status": "partial" if failed else "completed",
    }


def merge_duplicate_episodic(space_id: str, *, min_score: float = 0.72) -> dict[str, Any]:
    """Weekly consolidation pass: merge near-duplicate episodic memories by preserving sources."""
    episodic = list_memories(space_id, status="active", memory_type="episodic", limit=100)
    merged: list[dict[str, Any]] = []
    consumed: set[str] = set()
    for keeper in episodic:
        if keeper.id in consumed:
            continue
        for duplicate in episodic:
            if duplicate.id == keeper.id or duplicate.id in consumed:
                continue
            score = score_memory(keeper.content, duplicate)
            if score < min_score:
                continue
            for source in duplicate.sources:
                add_source(keeper.id, source.note_id, "supported_by")
            update_memory(duplicate.id, status="superseded", valid_until=utc_now_iso(), reason="weekly_duplicate_episodic_merge")
            consumed.add(duplicate.id)
            merged.append({"keeper_id": keeper.id, "merged_id": duplicate.id, "score": score})
    return {"space_id": space_id, "merged_count": len(merged), "merged": merged}


def generate_stable_semantic(space_id: str, *, min_sources: int = 3) -> dict[str, Any]:
    """Synthesize a stable semantic candidate from a generic episodic cluster."""
    episodic = list_memories(space_id, status="active", memory_type="episodic", limit=100)
    if len(episodic) < min_sources:
        return {"space_id": space_id, "created": False, "reason": "not_enough_sources", "source_count": len(episodic)}

    # Keep consolidation domain-neutral: the cluster is formed from structured
    # predicates when available, otherwise from the current episodic stream.
    grouped: dict[str, list[Any]] = {}
    for memory in episodic:
        group_key = memory.predicate or "episodic_stream"
        grouped.setdefault(group_key, []).append(memory)
    cluster = max(grouped.values(), key=lambda items: (len(items), max(item.updated_at for item in items)))
    if len(cluster) < min_sources:
        return {"space_id": space_id, "created": False, "reason": "no_stable_cluster", "source_count": len(cluster)}

    source_note_ids = list(dict.fromkeys(source.note_id for memory in cluster for source in memory.sources))
    source_note_ids = source_note_ids[:100]
    source_contents = list(dict.fromkeys(memory.content for memory in cluster))[:5]
    candidate = MemoryCandidate(
        memory_type="semantic",
        content="用户近期稳定主题：" + "；".join(source_contents),
        importance=0.9,
        confidence=0.82,
        entities=[],
        reason="monthly_generic_cluster_consolidation",
        space_id=space_id,
        subject="用户",
        predicate="stable_theme",
        object_value=cluster[0].predicate or "episodic_stream",
    )
    first_source = source_note_ids[0] if source_note_ids else cluster[0].id
    result = consolidate_candidate(space_id, first_source, candidate)
    memory_id = str(result.get("memory_id") or "")
    if not memory_id:
        return {"space_id": space_id, "created": False, "reason": "candidate_not_applied", "source_count": len(source_note_ids)}
    for note_id in source_note_ids[1:]:
        add_source(memory_id, note_id, "summarized_from")
    for source_memory in cluster:
        add_memory_relation(space_id, memory_id, source_memory.id, "summarized_from", decision_id=result.get("decision_id"))
    return {
        "space_id": space_id,
        "created": result.get("action") == "insert",
        "action": result.get("action"),
        "memory_id": memory_id,
        "source_count": len(source_note_ids),
    }


def run_monthly_semantic_consolidation(space_id: str, *, min_cluster_size: int = 3) -> dict[str, Any]:
    """Monthly semantic consolidation with deterministic safety gates.

    This pass is intentionally conservative: it groups active Memory by stable
    semantic keys and only creates a summarized semantic Memory when sources are
    sufficient and polarity/scope do not conflict.
    """
    memories = list_memories(space_id, status="active", limit=500)
    groups: dict[tuple[str, str], list[Any]] = {}
    for memory in memories:
        if memory.memory_type == "task":
            continue
        key = memory.effective_memory_key or memory.predicate or memory.normalized_content[:48]
        groups.setdefault((memory.memory_type, key), []).append(memory)

    reviewed = 0
    created = []
    skipped = []
    for (memory_type, key), cluster in groups.items():
        if len(cluster) < min_cluster_size:
            continue
        reviewed += 1
        polarities = {memory.polarity for memory in cluster if memory.polarity}
        if len(polarities) > 1:
            skipped.append({"memory_type": memory_type, "key": key, "reason": "polarity_conflict", "count": len(cluster)})
            continue
        source_note_ids = list(dict.fromkeys(source.note_id for memory in cluster for source in memory.sources))
        if len(source_note_ids) < min_cluster_size:
            skipped.append({"memory_type": memory_type, "key": key, "reason": "not_enough_sources", "count": len(source_note_ids)})
            continue
        contents = list(dict.fromkeys(memory.content for memory in cluster))[:5]
        candidate = MemoryCandidate(
            memory_type="semantic",
            content="用户长期稳定主题：" + "；".join(contents),
            importance=max(memory.importance for memory in cluster),
            confidence=min(0.9, max(memory.confidence for memory in cluster)),
            entities=[],
            reason="monthly_semantic_cluster_consolidation",
            space_id=space_id,
            subject="用户",
            predicate="stable_theme",
            object_value=key[:120],
        )
        result = consolidate_candidate(space_id, source_note_ids[0], candidate)
        memory_id = str(result.get("memory_id") or "")
        if not memory_id:
            skipped.append({"memory_type": memory_type, "key": key, "reason": "candidate_not_applied", "count": len(cluster)})
            continue
        for note_id in source_note_ids[1:]:
            add_source(memory_id, note_id, "summarized_from")
        for source_memory in cluster:
            add_memory_relation(space_id, memory_id, source_memory.id, "summarized_from", decision_id=result.get("decision_id"))
        created.append({"memory_id": memory_id, "memory_type": memory_type, "key": key, "source_count": len(source_note_ids)})

    if not settings.MONTHLY_SEMANTIC_CONSOLIDATION_ENABLED:
        fallback = generate_stable_semantic(space_id, min_sources=min_cluster_size)
        return {"space_id": space_id, "mode": "legacy_fallback", "semantic_reviewed": reviewed, "fallback": fallback}
    return {
        "space_id": space_id,
        "mode": "semantic_cluster",
        "reviewed_clusters": reviewed,
        "created_count": len(created),
        "created": created,
        "skipped": skipped[:50],
    }
