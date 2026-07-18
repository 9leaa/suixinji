"""Deterministic validation and privacy filtering for memory candidates."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from core.sensitive import contains_sensitive_data
from core.settings import MEMORY_CANDIDATE_MIN_CONFIDENCE
from memory.models import MemoryCandidate, memory_key_for, normalize_content


LOW_VALUE_TEXTS = {"你好", "您好", "hello", "hi", "收到", "好的", "好", "ok", "谢谢", "哈哈", "嗯", "嗯嗯"}


@dataclass(frozen=True)
class CandidateRejection:
    candidate_id: str
    reason: str


def _safe_score(value: float) -> float:
    score = float(value)
    if not math.isfinite(score):
        return 0.0
    return min(1.0, max(0.0, score))


def validate_candidate(candidate: MemoryCandidate, *, note_text: str = "") -> tuple[MemoryCandidate | None, CandidateRejection | None]:
    """Validate one candidate without allowing a model to mutate storage."""
    content = " ".join(str(candidate.content or "").split()).strip()
    normalized = normalize_content(content)
    if not candidate.should_store:
        return None, CandidateRejection(candidate.candidate_id, candidate.effective_reason or "candidate_should_not_store")
    if not content or len(normalized) <= 2 or normalized in {normalize_content(item) for item in LOW_VALUE_TEXTS}:
        return None, CandidateRejection(candidate.candidate_id, "low_value_content")
    if contains_sensitive_data(note_text) or contains_sensitive_data(content):
        return None, CandidateRejection(candidate.candidate_id, "sensitive_data")

    confidence = _safe_score(candidate.confidence)
    importance = _safe_score(candidate.importance)
    if confidence < MEMORY_CANDIDATE_MIN_CONFIDENCE:
        return None, CandidateRejection(candidate.candidate_id, "confidence_below_minimum")

    evidence_span = str(candidate.evidence_span or "").strip()
    if evidence_span and evidence_span not in note_text:
        evidence_span = ""
    validated = replace(
        candidate,
        content=content[:1000],
        confidence=confidence,
        importance=importance,
        entities=list(dict.fromkeys(str(item).strip() for item in candidate.entities if str(item).strip()))[:12],
        subject=(str(candidate.subject).strip()[:160] if candidate.subject else None),
        predicate=(str(candidate.predicate).strip()[:80] if candidate.predicate else None),
        object_value=(str(candidate.object_value).strip()[:240] if candidate.object_value else None),
        evidence_span=(evidence_span[:500] or None),
        memory_key=memory_key_for(
            candidate.memory_type,
            subject=candidate.subject,
            predicate=candidate.predicate,
            object_value=candidate.object_value,
            content=content[:1000],
        ),
    )
    return validated, None


def validate_candidates(
    candidates: list[MemoryCandidate],
    *,
    note_text: str = "",
) -> tuple[list[MemoryCandidate], list[CandidateRejection]]:
    valid: list[MemoryCandidate] = []
    rejected: list[CandidateRejection] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        checked, rejection = validate_candidate(candidate, note_text=note_text)
        if rejection is not None:
            rejected.append(rejection)
            continue
        if checked is None:
            continue
        key = (checked.memory_type, checked.normalized_content)
        if key in seen:
            rejected.append(CandidateRejection(checked.candidate_id, "duplicate_candidate"))
            continue
        seen.add(key)
        valid.append(checked)
    return valid, rejected
