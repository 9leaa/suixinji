"""Retrieve active memories that may relate to a candidate."""

from __future__ import annotations

from memory.models import MemoryCandidate, MemoryRecord
from memory.repository import list_memories
from memory.retriever import score_memory


def retrieve_candidates(space_id: str, candidate: MemoryCandidate, *, limit: int = 5) -> list[MemoryRecord]:
    memories = list_memories(space_id, status="active", memory_type=candidate.memory_type, limit=100)
    scored = [(memory, score_memory(candidate.content, memory)) for memory in memories]
    if any(marker in candidate.content for marker in ("搬到", "住在")):
        scored.extend(
            (memory, 0.6)
            for memory in memories
            if any(marker in memory.content for marker in ("搬到", "住在"))
        )
    scored.sort(key=lambda item: item[1], reverse=True)
    return [memory for memory, score in scored[:limit] if score > 0]
