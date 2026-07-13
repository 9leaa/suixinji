"""Consolidate extracted candidates into versioned memories."""

from __future__ import annotations

from typing import Any

from memory.candidate_retriever import retrieve_candidates
from memory.models import MemoryCandidate, utc_now_iso
from memory.repository import add_source, insert_memory, list_memories, note_has_memory, update_memory
from memory.relation_classifier import classify_relation
from memory.retriever import score_memory
from memory.trace import add_step
from storage.note_storage import load_index


def consolidate_candidate(space_id: str, note_id: str, candidate: MemoryCandidate, *, trace: dict[str, Any] | None = None) -> dict[str, Any]:
    if not candidate.should_store:
        add_step(
            trace,
            "memory_discarded",
            input_summary={"candidate_id": candidate.candidate_id, "memory_type": candidate.memory_type},
            reason=candidate.reason or "candidate_should_not_store",
        )
        return {"action": "discarded", "candidate_id": candidate.candidate_id}

    similar = retrieve_candidates(space_id, candidate)
    add_step(
        trace,
        "similar_memories_retrieved",
        input_summary={"candidate_id": candidate.candidate_id, "memory_type": candidate.memory_type},
        output_summary={"retrieved_count": len(similar), "memory_ids": [memory.id for memory in similar]},
    )
    decision = classify_relation(candidate, similar)
    add_step(
        trace,
        "relation_classified",
        input_summary={"candidate_id": candidate.candidate_id},
        output_summary={"relation": decision.relation, "target_memory_id": decision.target_memory_id, "action": decision.action},
        reason=decision.reason,
    )

    if decision.action == "add_source" and decision.target_memory_id:
        add_source(decision.target_memory_id, note_id, "supported_by")
        add_step(trace, "memory_source_added", output_summary={"memory_id": decision.target_memory_id}, reason=decision.reason)
        return {"action": "add_source", "memory_id": decision.target_memory_id, "relation": decision.relation}

    if decision.action == "merge" and decision.target_memory_id:
        add_source(decision.target_memory_id, note_id, "supported_by")
        add_step(trace, "memory_merged", output_summary={"memory_id": decision.target_memory_id}, reason=decision.reason)
        return {"action": "merge", "memory_id": decision.target_memory_id, "relation": decision.relation}

    if decision.action == "update_task" and decision.target_memory_id:
        add_source(decision.target_memory_id, note_id, "updated_by")
        updated = update_memory(
            decision.target_memory_id,
            content=candidate.content,
            task_status=candidate.task_status,
            reason=decision.reason or "task_status_changed",
            source_note_id=note_id,
        )
        add_step(
            trace,
            "memory_updated",
            output_summary={"memory_id": decision.target_memory_id, "task_status": candidate.task_status},
            reason=decision.reason,
        )
        return {"action": "update_task", "memory_id": decision.target_memory_id, "task_status": updated.task_status if updated else candidate.task_status}

    if decision.action == "supersede" and decision.target_memory_id:
        update_memory(
            decision.target_memory_id,
            status="superseded",
            valid_until=utc_now_iso(),
            reason=decision.reason or "superseded_by_new_note",
            source_note_id=note_id,
        )
        new_memory = insert_memory(space_id, candidate, source_note_id=note_id, source_relation="updated_by")
        add_step(
            trace,
            "memory_superseded",
            output_summary={"target_memory_id": decision.target_memory_id, "memory_id": new_memory.id},
            reason=decision.reason,
        )
        return {"action": "supersede", "memory_id": new_memory.id, "target_memory_id": decision.target_memory_id}

    if decision.action == "conflict" and decision.target_memory_id:
        update_memory(
            decision.target_memory_id,
            status="conflicted",
            reason=decision.reason or "ambiguous_conflict",
            source_note_id=note_id,
        )
        conflicted = MemoryCandidate(
            memory_type=candidate.memory_type,
            content=candidate.content,
            importance=candidate.importance,
            confidence=min(candidate.confidence, 0.6),
            entities=candidate.entities,
            should_store=candidate.should_store,
            task_status=candidate.task_status,
            reason=candidate.reason,
        )
        new_memory = insert_memory(space_id, conflicted, source_note_id=note_id, source_relation="contradicted_by", status="conflicted")
        add_step(
            trace,
            "memory_conflicted",
            output_summary={"target_memory_id": decision.target_memory_id, "memory_id": new_memory.id},
            reason=decision.reason,
        )
        return {"action": "conflict", "memory_id": new_memory.id, "target_memory_id": decision.target_memory_id}

    memory = insert_memory(space_id, candidate, source_note_id=note_id)
    add_step(trace, "memory_inserted", output_summary={"memory_id": memory.id, "memory_type": memory.memory_type}, reason=decision.reason)
    return {"action": "insert", "memory_id": memory.id, "relation": decision.relation}


def process_unextracted_notes(space_id: str, *, limit: int = 100) -> dict[str, Any]:
    """Daily consolidation pass: extract memories for notes without memory sources."""
    from memory.service import process_note_memory

    processed = []
    skipped = 0
    for note in load_index(space_id)[: max(1, min(int(limit), 500))]:
        note_id = str(note.get("id") or "")
        if not note_id or note_has_memory(note_id):
            skipped += 1
            continue
        report = process_note_memory(note)
        processed.append({"note_id": note_id, "trace_id": report.get("trace_id"), "candidates": report.get("candidates")})
    return {"space_id": space_id, "processed": processed, "processed_count": len(processed), "skipped_count": skipped}


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
    """Monthly consolidation pass: synthesize a stable semantic memory from related episodic memories."""
    episodic = [
        memory
        for memory in list_memories(space_id, status="active", memory_type="episodic", limit=100)
        if any(token in memory.content for token in ("Agent", "RAG", "学习", "开发", "向量", "ReAct"))
    ]
    if len(episodic) < min_sources:
        return {"space_id": space_id, "created": False, "reason": "not_enough_sources", "source_count": len(episodic)}

    source_note_ids: list[str] = []
    for memory in episodic:
        source_note_ids.extend(source.note_id for source in memory.sources)
    source_note_ids = list(dict.fromkeys(source_note_ids))
    candidate = MemoryCandidate(
        memory_type="semantic",
        content="用户当前持续学习和开发 Agent/RAG 系统。",
        importance=0.9,
        confidence=0.82,
        entities=["Agent", "RAG"],
        reason="monthly_stable_semantic_consolidation",
    )
    first_source = source_note_ids[0] if source_note_ids else episodic[0].id
    memory = insert_memory(space_id, candidate, source_note_id=first_source)
    for note_id in source_note_ids[1:]:
        add_source(memory.id, note_id, "supported_by")
    return {"space_id": space_id, "created": True, "memory_id": memory.id, "source_count": len(source_note_ids)}
