"""文件作用：Memory 候选抽取。

项目关系：本文件依赖 `core`、`core.config`、`core.llm_client`、`core.model_router` 等 14 个模块；被 `apps.handlers`、`eval.eval_memory`、`eval.eval_memory_quality`、`eval.resume_memory_system_benchmark` 等 9 个模块。
"""



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
from core.model_router import route_model
from core.observability import log_event
from core.sensitive import redact_sensitive_text
from memory.candidate_validator import contains_sensitive_data
from memory.canonicalizer import is_task_lifecycle_statement, normalize_candidate_v3
from memory.clause_splitter import split_clauses
from memory.extraction_schema import parse_extracted_candidate
from memory.models import MEMORY_TYPES, TASK_STATUSES, MemoryCandidate, candidate_id_for, candidate_id_for_evidence, memory_key_for
from memory.policies.preference import preference_polarity, preference_signature
from memory.prompts import MEMORY_EXTRACTOR_PROMPT, MEMORY_EXTRACTOR_V3_PROMPT
from memory.task_state import infer_task_status

LOGGER = logging.getLogger(__name__)
EXTRACTOR_VERSION = "memory-extractor-v1"
LLM_EXTRACTOR_VERSION = "memory-extractor-v1-llm"
V3_LLM_EXTRACTOR_VERSION = "memory-extractor-v3-llm"
PROMPT_HASH = hashlib.sha256(MEMORY_EXTRACTOR_PROMPT.encode("utf-8")).hexdigest()[:16]
V3_PROMPT_HASH = hashlib.sha256(MEMORY_EXTRACTOR_V3_PROMPT.encode("utf-8")).hexdigest()[:16]
MEMORY_EXTRACTOR_MODE = settings.MEMORY_EXTRACTOR_MODE
_ERROR_PREVIEW_RE = re.compile(r"\b(text_preview|output_preview)=('(?:\\'|[^'])*'|\"(?:\\\"|[^\"])*\")")
_DIAGNOSTIC_PREFIX_RE = re.compile(r"^\[MemoryV3-E2E-[^\]\n]{1,80}\]\s*")

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
    """函数功能：`_entities` 负责处理 entities，服务于本文件职责：Memory 候选抽取。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
    found = re.findall(r"[A-Za-z][A-Za-z0-9+#.-]*", text)
    for keyword in ("咖啡", "牛奶", "苹果", "北京", "上海", "Java", "Python", "Agent", "RAG", "README", "CI"):
        if keyword in text and keyword not in found:
            found.append(keyword)
    return found[:8]


def _task_status(text: str) -> str:
    """函数功能：`_task_status` 负责处理 task status，服务于本文件职责：Memory 候选抽取。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return infer_task_status(text) or "todo"


def _clean_subject(text: str) -> str:
    """函数功能：`_clean_subject` 负责清理 subject，服务于本文件职责：Memory 候选抽取。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    value = text.strip()
    value = re.sub(r"^(记得|需要|待办|todo[:：]?|帮我记一下|提醒我)", "", value, flags=re.IGNORECASE).strip(" ：:")
    value = re.sub(r"^(我|本人|用户)", "", value).strip(" ：:")
    return value or text.strip()


def _structured_fields(memory_type: str, text: str, entities: list[str]) -> tuple[str | None, str | None, str | None]:
    """函数功能：`_structured_fields` 负责处理 structured fields，服务于本文件职责：Memory 候选抽取。
    传参：
        memory_type: memory type 参数，由调用方传入，类型为 `str`。
        text: 输入文本内容，类型为 `str`。
        entities: entities 参数，由调用方传入，类型为 `list[str]`。
    返回结果说明：
        返回 `tuple[str | None, str | None, str | None]`，表示由多个相关值组成的结果。
    """
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
    """函数功能：`_should_skip_text` 负责处理 should skip text，服务于本文件职责：Memory 候选抽取。
    传参：
        raw: raw 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    compact = re.sub(r"\s+", "", raw).casefold()
    if not raw or compact in LOW_VALUE_PATTERNS or len(compact) <= 2:
        return True
    if contains_sensitive_data(raw):
        return True
    return any(token in raw for token in LOW_CONFIDENCE_HINTS) and "记住" not in raw


def may_contain_memory(text: str, classification: dict[str, Any] | None = None) -> bool:
    """函数功能：`may_contain_memory` 负责处理 may contain memory，服务于本文件职责：Memory 候选抽取。
    传参：
        text: 输入文本内容，类型为 `str`。
        classification: classification 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
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
        "做完",
        "已做完",
        "学完",
        "已学完",
        "弄好",
        "提醒",
        "计划",
        "报名",
        "正在",
        "重点",
        "学习",
        "研究",
        "开发",
        "负责",
        "更换",
        "换成",
        "改成",
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
    #只要文本长度达到24，也判定true
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
    scope: dict[str, Any] | None = None,
) -> MemoryCandidate:
    """函数功能：`_candidate` 负责处理 candidate，服务于本文件职责：Memory 候选抽取。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        memory_type: memory type 参数，由调用方传入，类型为 `str`。
        content: 需要处理、保存或展示的文本内容，类型为 `str`。
        importance: importance 参数，由调用方传入，类型为 `float`。
        confidence: confidence 参数，由调用方传入，类型为 `float`。
        entities: entities 参数，由调用方传入，类型为 `list[str]`。
        reason: reason 参数，由调用方传入，类型为 `str`。
        task_status: task status 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        evidence_span: evidence span 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        subject: subject 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        predicate: predicate 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        object_value: object value 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        valid_from: valid from 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        valid_until: valid until 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        should_store: should store 参数，由调用方传入，类型为 `bool`，默认值为 `True`。
        extractor_type: extractor type 参数，由调用方传入，类型为 `str`，默认值为 `'rules'`。
        extractor_version: extractor version 参数，由调用方传入，类型为 `str`，默认值为 `EXTRACTOR_VERSION`。
        model: model 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        clause_index: clause index 参数，由调用方传入，类型为 `int | None`，默认值为 `None`。
        scope: scope 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
    返回结果说明：
        返回 `MemoryCandidate` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    if subject is None and predicate is None and object_value is None:
        subject, predicate, object_value = _structured_fields(memory_type, evidence_span or content, entities)
    #看一下 偏好的积极还是消极，还是未知
    polarity = preference_polarity(evidence_span or content) if memory_type == "preference" else None
    #看一下这个candidate属于哪个稳定槽位或任务身份
    resolved_memory_key = memory_key_for(
        memory_type,
        subject=subject,
        predicate=predicate,
        object_value=object_value,
        content=content,
    )
    candidate = MemoryCandidate(
        memory_type=memory_type,
        content=content,
        importance=importance,
        confidence=confidence,
        entities=entities,
        should_store=should_store,
        task_status=task_status,
        reason=reason,
        #id包括有：字句的位置
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
        prompt_hash=(V3_PROMPT_HASH if extractor_type == "llm" and extractor_version == V3_LLM_EXTRACTOR_VERSION else PROMPT_HASH)
        if extractor_type == "llm"
        else None,
        scope=dict(scope or {}),
    )
    return normalize_candidate_v3(candidate, evidence_span or content) if settings.MEMORY_CANONICAL_KEY_V3_ENABLED else candidate


def extract_rule_candidates(note_id: str, text: str, classification: dict[str, Any] | None = None) -> list[MemoryCandidate]:
    """函数功能：`extract_rule_candidates` 负责抽取 rule candidates，服务于本文件职责：Memory 候选抽取。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        text: 输入文本内容，类型为 `str`。
        classification: classification 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[MemoryCandidate]`，表示按条件筛选、构造或查询得到的列表。
    """
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
    """函数功能：`_extract_rule_candidates_for_clause` 负责抽取 rule candidates for clause，服务于本文件职责：Memory 候选抽取。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        raw: raw 参数，由调用方传入，类型为 `str`。
        clause_index: clause index 参数，由调用方传入，类型为 `int | None`。
    返回结果说明：
        返回 `list[MemoryCandidate]`，表示按条件筛选、构造或查询得到的列表。
    """
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
    v3_lifecycle_task = settings.MEMORY_EXTRACTOR_SCHEMA_V3_ENABLED and is_task_lifecycle_statement(raw)
    has_task_marker = has_task_marker or v3_lifecycle_task
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

    if (
        not (settings.MEMORY_EXTRACTOR_SCHEMA_V3_ENABLED and has_task_marker)
        and (any(marker in raw for marker in semantic_markers) or ("项目" in raw and not has_task_marker) or any(
        pattern.search(raw) for pattern in SHORT_FACT_PATTERNS
        ))
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
    """函数功能：`_float_value` 负责处理 float value，服务于本文件职责：Memory 候选抽取。
    传参：
        value: 待转换、校验或计算的值，类型为 `Any`。
        default: default 参数，由调用方传入，类型为 `float`。
    返回结果说明：
        返回 `float`，表示计算得到的数值结果。
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool_value(value: Any, default: bool = True) -> bool:
    """函数功能：`_bool_value` 负责处理 bool value，服务于本文件职责：Memory 候选抽取。
    传参：
        value: 待转换、校验或计算的值，类型为 `Any`。
        default: default 参数，由调用方传入，类型为 `bool`，默认值为 `True`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().casefold() not in {"false", "0", "no", "off"}
    return bool(value)


def _strip_diagnostic_prefix(text: str) -> str:
    """函数功能：`_strip_diagnostic_prefix` 负责处理 strip diagnostic prefix，服务于本文件职责：Memory 候选抽取。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return _DIAGNOSTIC_PREFIX_RE.sub("", str(text or "").strip(), count=1).strip()


def _safe_exception_summary(exc: Exception) -> str:
    """函数功能：`_safe_exception_summary` 负责处理 safe exception summary，服务于本文件职责：Memory 候选抽取。
    传参：
        exc: 当前捕获的异常对象，类型为 `Exception`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    message = str(exc).strip() or type(exc).__name__
    message = _ERROR_PREVIEW_RE.sub(lambda match: f"{match.group(1)}=[redacted]", message)
    return redact_sensitive_text(message)[:500]


def _log_llm_failure(note_id: str, exc: Exception, *, mode: str) -> None:
    """函数功能：`_log_llm_failure` 负责记录日志 llm failure，服务于本文件职责：Memory 候选抽取。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        exc: 当前捕获的异常对象，类型为 `Exception`。
        mode: mode 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    route = route_model(task="memory_extraction")
    config = get_chat_config(route.role.value)
    error = _safe_exception_summary(exc)
    log_event(
        "memory.extractor.llm_failed",
        level="warning",
        status="degraded",
        record_id=note_id,
        error=f"{type(exc).__name__}: {error}",
        extra={
            "extractor_mode": mode,
            "schema_v3": bool(settings.MEMORY_EXTRACTOR_SCHEMA_V3_ENABLED),
            "fallback": "rules",
            "fallback_reason": "llm_failed_rule_fallback",
            "model_role": route.role.value,
            "model": config.model,
            "route_reason": route.reason,
            "error_type": type(exc).__name__,
        },
    )
    LOGGER.warning(
        "memory.extractor.llm_failed note_id=%s error_type=%s error=%s fallback=rules",
        note_id,
        type(exc).__name__,
        error,
    )


def extract_llm_candidates(
    note_id: str,
    text: str,
    classification: dict[str, Any] | None = None,
    *,
    hints: list[dict[str, Any]] | None = None,
) -> list[MemoryCandidate]:
    """函数功能：`extract_llm_candidates` 负责抽取 llm candidates，服务于本文件职责：Memory 候选抽取。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        text: 输入文本内容，类型为 `str`。
        classification: classification 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
        hints: hints 参数，由调用方传入，类型为 `list[dict[str, Any]] | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[MemoryCandidate]`，表示按条件筛选、构造或查询得到的列表。
    """
    raw = str(text or "").strip()
    model_text = _strip_diagnostic_prefix(raw)
    if _should_skip_text(model_text):
        return []
    payload = {
        "note_id": note_id,
        "text": model_text,
        "classification": classification or {},
        "hints": hints or [],
    }
    data = complete_json(
        system_prompt=MEMORY_EXTRACTOR_V3_PROMPT if settings.MEMORY_EXTRACTOR_SCHEMA_V3_ENABLED else MEMORY_EXTRACTOR_PROMPT,
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
        if settings.MEMORY_EXTRACTOR_SCHEMA_V3_ENABLED:
            structured = parse_extracted_candidate(row, raw)
            if structured is None:
                continue
            scope = {
                "canonical_topic": structured.canonical_topic,
                "operation": structured.operation,
                "old_value": structured.old_value,
                "new_value": structured.new_value,
                "scope": "global",
            }
            candidates.append(
                _candidate(
                    note_id,
                    structured.memory_type,
                    structured.content or structured.evidence_span,
                    importance=structured.importance,
                    confidence=structured.confidence,
                    entities=structured.entities,
                    reason=structured.extraction_reason or "v3_structured_llm_extraction",
                    task_status=structured.task_status,
                    evidence_span=structured.evidence_span,
                    subject=structured.entity,
                    predicate=structured.attribute,
                    object_value=structured.new_value or structured.attribute,
                    valid_from=structured.valid_from.isoformat() if structured.valid_from else None,
                    valid_until=structured.valid_until.isoformat() if structured.valid_until else None,
                    should_store=structured.should_store,
                    extractor_type="llm",
                    extractor_version=V3_LLM_EXTRACTOR_VERSION,
                    model=get_chat_config("fast").model,
                    scope=scope,
                )
            )
            continue
        memory_type = str(row.get("memory_type") or "").strip().lower()
        content = str(row.get("content") or "").strip()
        if memory_type not in MEMORY_TYPES or not content:
            continue
        task_status = str(row.get("task_status") or "").strip().lower() or None
        # 兼容尚未跟随新 prompt 切换的模型输出；持久化只使用四状态模型。
        if task_status == "in_progress":
            task_status = "todo"
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


def _rule_hints(note_id: str, text: str, classification: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """函数功能：`_rule_hints` 负责处理 rule hints，服务于本文件职责：Memory 候选抽取。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        text: 输入文本内容，类型为 `str`。
        classification: classification 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    return [
        {
            "memory_type": candidate.memory_type,
            "task_status": candidate.task_status,
            "subject": candidate.subject,
            "predicate": candidate.predicate,
            "evidence_span": candidate.evidence_span,
        }
        for candidate in extract_rule_candidates(note_id, _strip_diagnostic_prefix(text), classification)[:5]
    ]


def _dedupe(candidates: list[MemoryCandidate]) -> list[MemoryCandidate]:
    """函数功能：`_dedupe` 负责处理 dedupe，服务于本文件职责：Memory 候选抽取。
    传参：
        candidates: candidates 参数，由调用方传入，类型为 `list[MemoryCandidate]`。
    返回结果说明：
        返回 `list[MemoryCandidate]`，表示按条件筛选、构造或查询得到的列表。
    """
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
    """函数功能：`extract_candidates` 负责抽取 candidates，服务于本文件职责：Memory 候选抽取。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        text: 输入文本内容，类型为 `str`。
        classification: classification 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[MemoryCandidate]`，表示按条件筛选、构造或查询得到的列表。
    """
    mode = MEMORY_EXTRACTOR_MODE if MEMORY_EXTRACTOR_MODE in {"rules", "llm", "hybrid"} else "rules"
    rule_text = _strip_diagnostic_prefix(text)
    if mode == "rules":
        return extract_rule_candidates(note_id, rule_text, classification)

    try:
        model_candidates = extract_llm_candidates(
            note_id,
            text,
            classification,
            hints=_rule_hints(note_id, text, classification) if mode == "hybrid" else None,
        )
    except Exception as exc:
        _log_llm_failure(note_id, exc, mode=mode)
        return [
            replace(candidate, reason="llm_failed_rule_fallback", extraction_reason="llm_failed_rule_fallback")
            for candidate in extract_rule_candidates(note_id, rule_text, classification)
        ]

    if mode == "llm":
        return model_candidates
    # Hybrid 表示规则提示加一次权威结构化模型结果；返回 rules/LLM 并集会让一句话产生不兼容 memory 类型，是重复误合并的来源。
    if model_candidates:
        return model_candidates
    # 空模型响应不是语义决策；保留确定性准入 fallback，但不要把它和非空模型结果做并集。
    return [
        replace(candidate, reason="llm_empty_rule_fallback", extraction_reason="llm_empty_rule_fallback")
        for candidate in extract_rule_candidates(note_id, rule_text, classification)
    ]
