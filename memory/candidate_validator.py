"""文件作用：候选安全与质量验证。

项目关系：本文件依赖 `core`、`core.sensitive`、`core.settings`、`memory.canonicalizer` 等 5 个模块；被 `eval.resume_memory_system_benchmark`、`eval.run_massive_memory_benchmark`、`memory.extractor`、`memory.service` 等 5 个模块。
"""



from __future__ import annotations

import math
from dataclasses import dataclass, replace

from core import settings
from core.sensitive import contains_sensitive_data
from core.settings import MEMORY_CANDIDATE_MIN_CONFIDENCE
from memory.canonicalizer import canonicalize_candidate
from memory.models import MemoryCandidate, memory_key_for, normalize_content


LOW_VALUE_TEXTS = {"你好", "您好", "hello", "hi", "收到", "好的", "好", "ok", "谢谢", "哈哈", "嗯", "嗯嗯"}


@dataclass(frozen=True)
class CandidateRejection:
    """类功能：`CandidateRejection` 封装与“候选安全与质量验证”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    candidate_id: str
    reason: str


def _safe_score(value: float) -> float:
    """函数功能：`_safe_score` 负责评分 safe，服务于本文件职责：候选安全与质量验证。
    传参：
        value: 待转换、校验或计算的值，类型为 `float`。
    返回结果说明：
        返回 `float`，表示计算得到的数值结果。
    """
    score = float(value)
    if not math.isfinite(score):
        return 0.0
    return min(1.0, max(0.0, score))


def validate_candidate(candidate: MemoryCandidate, *, note_text: str = "") -> tuple[MemoryCandidate | None, CandidateRejection | None]:
    """函数功能：`validate_candidate` 负责校验 candidate，服务于本文件职责：候选安全与质量验证。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
        note_text: note text 参数，由调用方传入，类型为 `str`，默认值为 `''`。
    返回结果说明：
        返回 `tuple[MemoryCandidate | None, CandidateRejection | None]`，表示由多个相关值组成的结果。
    """
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
        if settings.MEMORY_EXTRACTOR_SCHEMA_V3_ENABLED:
            return None, CandidateRejection(candidate.candidate_id, "evidence_span_not_grounded")
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
    if settings.MEMORY_CANONICAL_KEY_V3_ENABLED:
        validated = canonicalize_candidate(validated)
        if validated.memory_type == "task" and not all(
            (
                validated.subject,
                validated.predicate,
                validated.scope.get("operation"),
                validated.task_status,
            )
        ):
            return None, CandidateRejection(candidate.candidate_id, "task_identity_incomplete")
    return validated, None


def validate_candidates(
    candidates: list[MemoryCandidate],
    *,
    note_text: str = "",
) -> tuple[list[MemoryCandidate], list[CandidateRejection]]:
    """函数功能：`validate_candidates` 负责校验 candidates，服务于本文件职责：候选安全与质量验证。
    传参：
        candidates: candidates 参数，由调用方传入，类型为 `list[MemoryCandidate]`。
        note_text: note text 参数，由调用方传入，类型为 `str`，默认值为 `''`。
    返回结果说明：
        返回 `tuple[list[MemoryCandidate], list[CandidateRejection]]`，表示由多个相关值组成的结果。
    """
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
