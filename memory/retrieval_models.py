"""Explainable Memory retrieval hit models."""

from __future__ import annotations

from dataclasses import dataclass, field

from memory.models import MemoryRecord


@dataclass
class MemoryRetrievalHit:
    memory: MemoryRecord
    exact_rank: int | None = None
    structured_rank: int | None = None
    fts_rank: int | None = None
    trigram_rank: int | None = None
    vector_rank: int | None = None
    exact_score: float = 0.0
    structured_score: float = 0.0
    fts_score: float = 0.0
    trigram_score: float = 0.0
    vector_score: float = 0.0
    rrf_score: float = 0.0
    policy_score: float = 0.0
    final_score: float = 0.0
    reasons: list[str] = field(default_factory=list)
