"""Memory candidate extraction with deterministic and model-assisted modes."""

from __future__ import annotations

import json
import hashlib
import logging
import re
from dataclasses import replace
from typing import Any

from core.llm_client import complete_json
from core.config import get_chat_config
from core import settings
from memory.candidate_validator import contains_sensitive_data
from memory.clause_splitter import split_clauses
from memory.models import MEMORY_TYPES, TASK_STATUSES, MemoryCandidate, candidate_id_for, candidate_id_for_evidence, memory_key_for
from memory.policies.preference import preference_polarity, preference_signature
from memory.prompts import MEMORY_EXTRACTOR_PROMPT

LOGGER = logging.getLogger(__name__)
EXTRACTOR_VERSION = "memory-extractor-v1"
LLM_EXTRACTOR_VERSION = "memory-extractor-v1-llm"
PROMPT_HASH = hashlib.sha256(MEMORY_EXTRACTOR_PROMPT.encode("utf-8")).hexdigest()[:16]
MEMORY_EXTRACTOR_MODE = settings.MEMORY_EXTRACTOR_MODE

LOW_VALUE_PATTERNS = {
    "你好",
    "hello",
    "hi",
    "收到",
    "好的",
    "ok",
    "哈哈",
    "嗯",
    "嗯嗯",
    "今天天气不错",
}
LOW_CONFIDENCE_HINTS = ("可能", "也许", "大概", "好像", "猜一下")
SHORT_FACT_PATTERNS = (
    re.compile(r"^(?:我|本人|用户)是[^。！？!?；;，,]{1,40}$"),
    re.compile(r"^(?:我|本人|用户)姓[^。！？!?；;，,]{1,12}$"),
    re.compile(r"^(?:我|本人|用户)(?:来自|有|养了|会|不会)[^。！？!?；;，,]{1,40}$"),
    re.compile(r"^(?:我|本人|用户)在[^。！？!?；;，,]{1,40}工作$"),
    re.compile(r"^(?:我|本人|用户)的[^。！？!?；;，,]{1,30}是[^。！？!?；;，,]{1,40}$"),
    re.compile(r"^[^。！？!?；;，,]{1,30}是(?:我|本人|用户)的[^。！？!?；;，,]{1,20}$"),
)


def _entities(text: str) -> list[str]:
    found = re.findall(r"[A-Za-z][A-Za-z0-9+#.-]*", text)
    for keyword in ("咖啡", "牛奶", "苹果", "北京", "上海", "Java", "Python", "Agent", "RAG", "README", "CI"):
        if keyword in text and keyword not in found:
            found.append(keyword)
    return found[:8]


def _task_status(text: str) -> str:
    if any(token in text for token in ("取消", "不用做", "不做了")):
        return "cancelled"
    if any(token in text for token in ("完成", "搞定", "已做完", "做完")):
        return "done"
    if any(token in text for token in ("卡住", "阻塞", "等确认")):
        return "blocked"
    if any(token in text for token in ("正在", "进行中", "继续")):
        return "in_progress"
    if "准备" in text and any(token in text for token in ("最近", "现在", "重点")):
        return "in_progress"
    return "todo"


def _clean_subject(text: str) -> str:
    value = text.strip()
    value = re.sub(r"^(记得|需要|待办|todo[:：]?|帮我记一下|提醒我)", "", value, flags=re.IGNORECASE).strip(" ：:")
    value = re.sub(r"^(我|本人|用户)", "", value).strip(" ：:")
    return value or text.strip()


def _structured_fields(memory_type: str, text: str, entities: list[str]) -> tuple[str | None, str | None, str | None]:
    cleaned = _clean_subject(text)
    if memory_type == "preference":
        signature = preference_signature(text)
        return "用户", "preference", signature.topic or cleaned
    if memory_type == "task":
        subject = entities[0] if entities else cleaned
        return subject, "task", cleaned
    if memory_type == "semantic":
        if any(marker in text for marker in ("住在", "搬到", "居住")):
            location = next((item for item in reversed(entities) if item in {"北京", "上海"}), entities[-1] if entities else cleaned)
            return "用户", "location", location
        if any(marker in text for marker in ("学习", "只学", "重点", "研究")):
            return "用户", "learning_focus", " ".join(entities) or cleaned
        if any(marker in text for marker in ("开发", "负责", "项目")):
            return "用户", "current_project", " ".join(entities) or cleaned
        return "用户", "fact", cleaned
    if memory_type == "episodic":
        return "用户", "event", cleaned
    return None, None, None


def _should_skip_text(raw: str) -> bool:
    compact = re.sub(r"\s+", "", raw).casefold()
    if not raw or compact in LOW_VALUE_PATTERNS or len(compact) <= 2:
        return True
    if contains_sensitive_data(raw):
        return True
    return any(token in raw for token in LOW_CONFIDENCE_HINTS) and "记住" not in raw


def may_contain_memory(text: str, classification: dict[str, Any] | None = None) -> bool:
    """Cheap admission check used before scheduling the Memory Worker."""
    raw = str(text or "").strip()
    if _should_skip_text(raw):
        return False
    metadata = classification or {}
    tags = " ".join(str(item) for item in metadata.get("tags") or [])
    searchable = " ".join([raw, str(metadata.get("title") or ""), str(metadata.get("summary") or ""), tags])
    markers = (
        "喜欢",
        "不喜欢",
        "更喜欢",
        "讨厌",
        "暂时不",
        "不喝",
        "不吃",
        "不用",
        "偏好",
        "习惯",
        "过敏",
        "记得",
        "需要",
        "待办",
        "todo",
        "跟进",
        "完成",
        "提醒",
        "计划",
        "报名",
        "正在",
        "重点",
        "学习",
        "研究",
        "开发",
        "负责",
        "住在",
        "搬到",
        "今天",
        "昨天",
        "刚才",
        "参加",
        "发布",
    )
    if any(marker in searchable for marker in markers):
        return True
    if any(pattern.search(raw) for pattern in SHORT_FACT_PATTERNS):
        return True
    return len(raw) >= 24


def _candidate(
    note_id: str,
    memory_type: str,
    content: str,
    *,
    importance: float,
    confidence: float,
    entities: list[str],
    reason: str,
    task_status: str | None = None,
    evidence_span: str | None = None,
    subject: str | None = None,
    predicate: str | None = None,
    object_value: str | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    should_store: bool = True,
    extractor_type: str = "rules",
    extractor_version: str = EXTRACTOR_VERSION,
    model: str | None = None,
    clause_index: int | None = None,
) -> MemoryCandidate:
    if subject is None and predicate is None and object_value is None:
        subject, predicate, object_value = _structured_fields(memory_type, evidence_span or content, entities)
    polarity = preference_polarity(evidence_span or content) if memory_type == "preference" else None
    resolved_memory_key = memory_key_for(
        memory_type,
        subject=subject,
        predicate=predicate,
        object_value=object_value,
        content=content,
    )
    return MemoryCandidate(
        memory_type=memory_type,
        content=content,
        importance=importance,
        confidence=confidence,
        entities=entities,
        should_store=should_store,
        task_status=task_status,
        reason=reason,
        candidate_id=(
            candidate_id_for_evidence(
                note_id,
                memory_type,
                content,
                memory_key=resolved_memory_key,
                evidence_span=evidence_span,
                clause_index=clause_index,
            )
            if clause_index is not None
            else candidate_id_for(note_id, memory_type, content)
        ),
        note_id=note_id,
        subject=subject,
        predicate=predicate,
        object_value=object_value,
        valid_from=valid_from,
        valid_until=valid_until,
        evidence_span=evidence_span,
        clause_index=clause_index,
        extraction_reason=reason,
        memory_key=resolved_memory_key,
        polarity=polarity,
        extractor_type=extractor_type,
        extractor_version=extractor_version,
        model=model,
        prompt_hash=PROMPT_HASH if extractor_type == "llm" else None,
    )


def extract_rule_candidates(note_id: str, text: str, classification: dict[str, Any] | None = None) -> list[MemoryCandidate]:
    """Extract candidates locally; this is also the model failure fallback."""
    del classification
    raw = str(text or "").strip()
    if _should_skip_text(raw):
        return []
    if settings.MEMORY_CLAUSE_EXTRACTION_ENABLED:
        return _dedupe(
            candidate
            for clause in split_clauses(raw)
            for candidate in _extract_rule_candidates_for_clause(note_id, clause.text, clause.index)
        )

    return _extract_rule_candidates_for_clause(note_id, raw, None)


def _extract_rule_candidates_for_clause(note_id: str, raw: str, clause_index: int | None) -> list[MemoryCandidate]:
    raw = str(raw or "").strip()
    if _should_skip_text(raw):
        return []

    entities = _entities(raw)
    candidates: list[MemoryCandidate] = []
    preference_markers = (
        "喜欢",
        "不喜欢",
        "更喜欢",
        "讨厌",
        "厌恶",
        "不爱",
        "偏好",
        "习惯",
        "不想",
        "不打算",
        "暂时不",
        "只学",
        "重点放在",
        "过敏",
    )
    task_markers = ("记得", "需要", "待办", "todo", "跟进", "修", "改", "实现", "完成", "提醒", "准备", "计划", "报名")
    semantic_markers = ("正在", "重点", "学习", "研究", "开发", "负责", "住在", "搬到", "使用", "采用")

    if any(marker in raw for marker in preference_markers):
        candidates.append(
            _candidate(
                note_id,
                "preference",
                f"用户{_clean_subject(raw)}",
                importance=0.75,
                confidence=0.86,
                entities=entities,
                reason="preference_marker",
                evidence_span=raw,
                clause_index=clause_index,
            )
        )

    has_task_marker = any(marker in raw.casefold() for marker in task_markers)
    if has_task_marker:
        candidates.append(
            _candidate(
                note_id,
                "task",
                _clean_subject(raw),
                importance=0.8,
                confidence=0.82,
                entities=entities,
                task_status=_task_status(raw),
                reason="task_marker",
                evidence_span=raw,
                clause_index=clause_index,
            )
        )

    if any(marker in raw for marker in semantic_markers) or ("项目" in raw and not has_task_marker) or any(
        pattern.search(raw) for pattern in SHORT_FACT_PATTERNS
    ):
        candidates.append(
            _candidate(
                note_id,
                "semantic",
                f"用户{_clean_subject(raw)}",
                importance=0.78,
                confidence=0.84,
                entities=entities,
                reason="semantic_marker",
                evidence_span=raw,
                clause_index=clause_index,
            )
        )

    if (len(raw) >= 12 or clause_index is not None) and any(
        marker in raw for marker in ("今天", "昨天", "刚才", "完成了", "去了", "参加", "发布")
    ):
        candidates.append(
            _candidate(
                note_id,
                "episodic",
                raw,
                importance=0.55,
                confidence=0.72,
                entities=entities,
                reason="episodic_event",
                evidence_span=raw,
                clause_index=clause_index,
            )
        )
    return _dedupe(candidates)


def _float_value(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool_value(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().casefold() not in {"false", "0", "no", "off"}
    return bool(value)


def extract_llm_candidates(note_id: str, text: str, classification: dict[str, Any] | None = None) -> list[MemoryCandidate]:
    raw = str(text or "").strip()
    if _should_skip_text(raw):
        return []
    payload = {
        "note_id": note_id,
        "text": raw,
        "classification": classification or {},
    }
    data = complete_json(
        system_prompt=MEMORY_EXTRACTOR_PROMPT,
        user_prompt=json.dumps(payload, ensure_ascii=False),
        llm_task="memory_extraction",
    )
    rows = data.get("candidates") or []
    if not isinstance(rows, list):
        raise ValueError("memory extractor candidates must be a list")

    candidates: list[MemoryCandidate] = []
    for row in rows[:5]:
        if not isinstance(row, dict):
            continue
        memory_type = str(row.get("memory_type") or "").strip().lower()
        content = str(row.get("content") or "").strip()
        if memory_type not in MEMORY_TYPES or not content:
            continue
        task_status = str(row.get("task_status") or "").strip().lower() or None
        if task_status not in TASK_STATUSES:
            task_status = None
        entities = row.get("entities") if isinstance(row.get("entities"), list) else []
        reason = str(row.get("extraction_reason") or row.get("reason") or "llm_extraction")[:240]
        candidates.append(
            _candidate(
                note_id,
                memory_type,
                content,
                importance=_float_value(row.get("importance"), 0.6),
                confidence=_float_value(row.get("confidence"), 0.6),
                entities=[str(item) for item in entities],
                reason=reason,
                task_status=task_status,
                evidence_span=str(row.get("evidence_span") or "") or None,
                subject=str(row.get("subject") or "") or None,
                predicate=str(row.get("predicate") or "") or None,
                object_value=str(row.get("object") or row.get("object_value") or "") or None,
                valid_from=str(row.get("valid_from") or "") or None,
                valid_until=str(row.get("valid_until") or "") or None,
                should_store=_bool_value(row.get("should_store"), True),
                extractor_type="llm",
                extractor_version=LLM_EXTRACTOR_VERSION,
                model=get_chat_config("fast").model,
                clause_index=int(row["clause_index"]) if str(row.get("clause_index") or "").isdigit() else None,
            )
        )
    return _dedupe(candidates)


def _dedupe(candidates: list[MemoryCandidate]) -> list[MemoryCandidate]:
    deduped: list[MemoryCandidate] = []
    seen: set[tuple[Any, ...]] = set()
    for candidate in candidates:
        key = (
            candidate.note_id,
            candidate.clause_index,
            candidate.memory_type,
            candidate.effective_memory_key,
            candidate.evidence_span or candidate.normalized_content,
        )
        if key not in seen:
            deduped.append(candidate)
            seen.add(key)
    return deduped


def extract_candidates(note_id: str, text: str, classification: dict[str, Any] | None = None) -> list[MemoryCandidate]:
    """Return candidates only; adjudication and database mutation happen later."""
    mode = MEMORY_EXTRACTOR_MODE if MEMORY_EXTRACTOR_MODE in {"rules", "llm", "hybrid"} else "rules"
    if mode == "rules":
        return extract_rule_candidates(note_id, text, classification)

    try:
        model_candidates = extract_llm_candidates(note_id, text, classification)
    except Exception as exc:
        LOGGER.warning("memory.extractor.llm_failed note_id=%s error_type=%s", note_id, type(exc).__name__)
        return [
            replace(candidate, reason="llm_failed_rule_fallback", extraction_reason="llm_failed_rule_fallback")
            for candidate in extract_rule_candidates(note_id, text, classification)
        ]

    if mode == "llm":
        return model_candidates
    return _dedupe(model_candidates + extract_rule_candidates(note_id, text, classification))
