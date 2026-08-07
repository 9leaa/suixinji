"""文件作用：Memory 候选抽取。

项目关系：本文件依赖 `core`、`core.config`、`core.llm_client`、`core.model_router` 等 14 个模块；被 `apps.handlers`、`eval.eval_memory`、`eval.eval_memory_quality`、`eval.resume_memory_system_benchmark` 等 9 个模块。
"""



from __future__ import annotations

import json
import hashlib
import logging
import re
import time
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
from memory.field_contracts import preference_scope_with_source, task_closure_reason, task_progress_metadata
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
LAST_EXTRACTION_DIAGNOSTICS: dict[str, Any] = {}
_LLM_CONNECTION_CIRCUIT_OPEN_UNTIL = 0.0
_ERROR_PREVIEW_RE = re.compile(r"\b(text_preview|output_preview)=('(?:\\'|[^'])*'|\"(?:\\\"|[^\"])*\")")
_DIAGNOSTIC_PREFIX_RE = re.compile(r"^\[MemoryV3-E2E-[^\]\n]{1,80}\]\s*")
_REFERENCE_SIGNAL_RE = re.compile(r"(?:这个|那个|它|这件事|上面那个|继续做|也(?:做完|完成)|取消它|就按这个)")

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
    polarity: str | None = None,
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
    # Rules infer polarity from evidence. LLM candidates carry their explicit
    # structured value; only an absent/invalid value falls back to inference.
    if memory_type == "preference":
        polarity = polarity if polarity in {"positive", "negative", "unknown"} else preference_polarity(evidence_span or content)
    else:
        polarity = None
    #看一下这个candidate属于哪个稳定槽位或任务身份
    resolved_memory_key = memory_key_for(
        memory_type,
        subject=subject,
        predicate=predicate,
        object_value=object_value,
        content=content,
    )
    resolved_scope = dict(scope or {})
    if memory_type == "preference":
        scope_value, scope_source = preference_scope_with_source(
            evidence_span or content,
            resolved_scope.get("scope"),
        )
        resolved_scope["scope"] = scope_value or "global"
        resolved_scope["scope_explicit"] = bool(scope_value)
        resolved_scope["scope_source"] = scope_source or "default"
    if memory_type == "task":
        source_text = evidence_span or content
        resolved_scope.update(task_progress_metadata(source_text))
        closure_reason = task_closure_reason(source_text=source_text)
        if task_status == "done" and closure_reason:
            resolved_scope["closure_reason"] = closure_reason
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
        scope=resolved_scope,
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
    # Normalize conversational wrappers before clause boundaries are found;
    # otherwise commas around fillers create orphan clauses and lose the
    # actual task assertion.
    raw = re.sub(r"^(?:嗯|呃|那个|，|,|…|\s)+", "", raw)
    raw = re.sub(r"^(?:先忽略前面的闲聊|总之)[，,]?\s*", "", raw)
    # Mixed notes must be atomized even when the legacy feature flag is off.
    # The flag remains useful for forcing clause metadata on a single clause,
    # but it must not allow a comma-delimited note to collapse into one task.
    clauses = split_clauses(raw)
    if settings.MEMORY_CLAUSE_EXTRACTION_ENABLED or len(clauses) > 1:
        return _dedupe(
            candidate
            for clause in clauses
            for candidate in _extract_rule_candidates_for_clause(note_id, clause.text, clause.index)
        )

    # Even when clause extraction is disabled, terminal sentence punctuation
    # is a boundary marker rather than evidence content.  For a mixed note,
    # retain an event clause alongside preference/task clauses; a task-only
    # sentence still must not manufacture an episodic memory.
    candidates = _extract_rule_candidates_for_clause(note_id, raw.rstrip("。！？!?；;"), None)
    if any(marker in raw for marker in ("今天", "昨天", "刚才", "参加", "去了", "发布")) and any(
        marker in raw for marker in ("喜欢", "偏好", "不喜欢", "更喜欢")
    ) and any(marker in raw for marker in ("要", "需要", "报名", "完成", "待办", "计划")):
        for clause in split_clauses(raw):
            if any(marker in clause.text for marker in ("今天", "昨天", "刚才", "参加", "去了", "发布")):
                candidates.extend(
                    candidate
                    for candidate in _extract_rule_candidates_for_clause(note_id, clause.text, clause.index)
                    if candidate.memory_type == "episodic"
                )
    return _dedupe(candidates)


def _extract_atomic_rule_candidates(note_id: str, text: str) -> list[MemoryCandidate]:
    return _dedupe(
        candidate
        for clause in split_clauses(str(text or ""))
        for candidate in _extract_rule_candidates_for_clause(note_id, clause.text, clause.index)
    )


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

    # Keep the meaningful evidence span bounded when conversational fillers
    # surround an assertion; filler is not a memory claim.
    raw = re.sub(r"^(?:嗯|呃|那个|，|,|…|\s)+", "", raw)
    raw = re.sub(r"^(?:先忽略前面的闲聊|总之)[，,]?\s*", "", raw)
    raw = re.sub(r"(?:，|,)?\s*(?:先别急|别急)[。！？!?]*$", "", raw).strip()

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
        "偏向",
        "习惯",
        "不想",
        "不打算",
        "暂时不",
        "只学",
        "重点放在",
        "过敏",
    )
    task_markers = ("记得", "需要", "待办", "todo", "跟进", "修", "改", "实现", "完成", "提交", "上线", "取消", "不做了", "不用做", "放弃", "等待", "还在等", "提醒", "准备", "计划", "报名", "开始", "处理", "继续做", "补充", "卡住", "阻塞", "暂停")
    semantic_markers = ("正在", "重点", "学习", "研究", "开发", "负责", "住在", "搬到", "使用", "采用")

    if any(marker in raw for marker in preference_markers):
        preference_spans = _preference_evidence_spans(raw)
        clause_polarity = preference_polarity(raw)
        for preference_span in preference_spans:
            preference_candidate = _candidate(
                    note_id,
                    "preference",
                    f"用户{_clean_subject(preference_span)}",
                    importance=0.75,
                    confidence=0.86,
                    entities=_entities(preference_span),
                    reason="preference_marker",
                    evidence_span=preference_span,
                    clause_index=clause_index,
                    scope={"atom_id": f"a{(clause_index or 0) + 1}"},
                )
            if preference_candidate.polarity == "unknown" and clause_polarity != "unknown":
                scope = dict(preference_candidate.scope or {})
                scope["polarity"] = clause_polarity
                preference_candidate = replace(preference_candidate, polarity=clause_polarity, scope=scope)
            candidates.append(preference_candidate)

    has_task_marker = any(marker in raw.casefold() for marker in task_markers)
    has_progress_task = bool(re.search(r"正在[^，,。！？!?；;]*(?:测试|处理|补充|开发|完善|修复)", raw))
    has_task_marker = has_task_marker or has_progress_task
    v3_lifecycle_task = settings.MEMORY_EXTRACTOR_SCHEMA_V3_ENABLED and is_task_lifecycle_statement(raw)
    has_task_marker = has_task_marker or v3_lifecycle_task
    has_date = any(marker in raw for marker in ("今天", "昨天", "前天", "刚才"))
    has_event_signal = any(marker in raw for marker in ("参加", "去了", "面试", "讨论")) or (
        has_date and "完成" in raw and "开始处理" not in raw
    )
    if has_task_marker and not has_event_signal:
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
                scope={"atom_id": f"a{(clause_index or 0) + 1}"},
            )
        )

    if (
        (not any(marker in raw for marker in preference_markers) or any(marker in raw for marker in ("重点放在", "主要使用", "正在开发", "负责")))
        and not (settings.MEMORY_EXTRACTOR_SCHEMA_V3_ENABLED and has_task_marker)
        and not has_event_signal
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
                evidence_span=re.sub(r"^(?:我|本人|用户)\s*", "", raw),
                clause_index=clause_index,
                scope={"atom_id": f"a{(clause_index or 0) + 1}"},
            )
        )

    if (has_event_signal or not has_task_marker) and (len(raw) >= 12 or clause_index is not None or any(
        marker in raw for marker in ("今天", "昨天", "刚才", "参加", "去了", "发布")
    )) and any(
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
                evidence_span=re.sub(r"^(?:我|本人|用户)\s*", "", raw),
                clause_index=clause_index,
                scope={"atom_id": f"a{(clause_index or 0) + 1}"},
            )
        )
    return _dedupe(candidates)


def _preference_evidence_spans(text: str) -> list[str]:
    """Split only clearly independent preference objects; keep scopes intact."""
    raw = str(text or "").strip()
    match = re.search(r"((?:不|更)?(?:喜欢|偏好|偏向|讨厌|不爱))\s*([^，,。；;]+)", raw)
    if not match:
        return [raw]
    marker, objects = match.group(1), match.group(2).strip()
    # Scope-bearing phrases are one assertion; do not split their qualifiers.
    if any(scope_marker in objects for scope_marker in ("工作日", "早上", "晚上", "时候", "情况下")):
        return [raw]
    parts = [part.strip() for part in re.split(r"\s*(?:、|和|以及)\s*", objects) if part.strip()]
    if len(parts) <= 1 or len(parts) > 6 or any(len(part) > 32 for part in parts):
        return [raw]
    spans: list[str] = []
    for index, part in enumerate(parts):
        evidence = f"{marker}{part}" if index == 0 else part
        if evidence in raw:
            spans.append(evidence)
    return spans or [raw]


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
    previous_messages: list[dict[str, Any]] | None = None,
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
    if MEMORY_EXTRACTOR_MODE == "hybrid" and time.monotonic() < _LLM_CONNECTION_CIRCUIT_OPEN_UNTIL:
        raise RuntimeError("LLM extraction circuit open; fallback=rules; error_category=connection_error")
    raw = str(text or "").strip()
    LAST_EXTRACTION_DIAGNOSTICS.update({"llm_called": True, "llm_schema_rejected_count": 0, "llm_candidate_count": 0, "llm_evidence_mapping_rejected_count": 0})
    model_text = _strip_diagnostic_prefix(raw)
    if _should_skip_text(model_text):
        return []
    payload = {
        "note_id": note_id,
        "text": model_text,
        "classification": classification or {},
        "hints": hints or [],
        "atoms": [
            {"atom_id": f"a{clause.index + 1}", "text": clause.text}
            for clause in split_clauses(model_text)
        ],
        "previous_messages": list(previous_messages or [])[:3] if _REFERENCE_SIGNAL_RE.search(model_text) else [],
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
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if settings.MEMORY_EXTRACTOR_SCHEMA_V3_ENABLED:
            structured = parse_extracted_candidate(row, raw)
            if structured is None:
                LAST_EXTRACTION_DIAGNOSTICS["llm_schema_rejected_count"] = int(LAST_EXTRACTION_DIAGNOSTICS.get("llm_schema_rejected_count") or 0) + 1
                log_event(
                    "memory.extractor.schema_rejected",
                    level="warning",
                    status="discarded",
                    record_id=note_id,
                    extra={"row_index": row_index, "reason": "invalid_schema_or_ungrounded_evidence"},
                )
                continue
            clause_index = _clause_index_for_evidence(model_text, structured.evidence_span)
            # Sentence punctuation is a boundary marker, not part of the
            # asserted evidence.  Keep the span grounded in the current
            # message while making rule/LLM paths use the same convention.
            evidence_span = str(structured.evidence_span or "").rstrip("。！？!?；;")
            scope = {
                "canonical_topic": structured.canonical_topic,
                "operation": structured.operation,
                "old_value": structured.old_value,
                "new_value": structured.new_value,
                "scope": structured.scope,
                "atom_id": f"a{clause_index + 1}" if clause_index is not None else None,
                "reference_status": structured.reference_status,
                "antecedent_note_id": structured.antecedent_note_id,
                "antecedent_offset": structured.antecedent_offset,
                "antecedent_evidence_span": structured.antecedent_evidence_span,
                "resolution_confidence": structured.resolution_confidence,
            }
            candidates.append(
                _candidate(
                    note_id,
                    structured.memory_type,
                    structured.content or evidence_span,
                    importance=structured.importance,
                    confidence=structured.confidence,
                    entities=structured.entities,
                    reason=structured.extraction_reason or "v3_structured_llm_extraction",
                    task_status=structured.task_status,
                    evidence_span=evidence_span,
                    subject=structured.entity,
                    predicate=structured.attribute,
                    object_value=structured.new_value or structured.attribute,
                    valid_from=structured.valid_from.isoformat() if structured.valid_from else None,
                    valid_until=structured.valid_until.isoformat() if structured.valid_until else None,
                    should_store=structured.should_store,
                    extractor_type="llm",
                    extractor_version=V3_LLM_EXTRACTOR_VERSION,
                    model=get_chat_config("fast").model,
                    clause_index=clause_index,
                    polarity=structured.polarity,
                    scope=scope,
                )
            )
            LAST_EXTRACTION_DIAGNOSTICS["llm_candidate_count"] = int(LAST_EXTRACTION_DIAGNOSTICS.get("llm_candidate_count") or 0) + 1
            continue
        memory_type = str(row.get("memory_type") or "").strip().lower()
        content = str(row.get("content") or "").strip()
        if memory_type not in MEMORY_TYPES or not content:
            continue
        task_status = str(row.get("task_status") or "").strip().lower() or None
        # 兼容尚未跟随新 prompt 切换的模型输出；新写入只使用 todo/done。
        task_status = {
            "in_progress": "todo", "blocked": "todo", "cancelled": "done", "canceled": "done"
        }.get(task_status, task_status)
        if task_status not in TASK_STATUSES:
            task_status = None
        entities = row.get("entities") if isinstance(row.get("entities"), list) else []
        reason = str(row.get("extraction_reason") or row.get("reason") or "llm_extraction")[:240]
        evidence_span = str(row.get("evidence_span") or "").rstrip("。！？!?；;") or None
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
                evidence_span=evidence_span,
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
                polarity=str(row.get("polarity") or "").strip().casefold() or None,
                scope={"scope": str(row.get("scope") or "").strip() or None},
            )
        )
        LAST_EXTRACTION_DIAGNOSTICS["llm_candidate_count"] = int(LAST_EXTRACTION_DIAGNOSTICS.get("llm_candidate_count") or 0) + 1
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
        for candidate in _extract_atomic_rule_candidates(note_id, _strip_diagnostic_prefix(text))
    ]


def _reference_antecedent_task(
    note_id: str,
    previous_messages: list[dict[str, Any]] | None,
) -> MemoryCandidate | None:
    """Resolve one recent user task for a short current reference.

    This is an identity-only fallback: the antecedent text is never emitted
    as a second candidate or persisted as a new fact. It only supplies the
    task identity that the current utterance updates.
    """
    usable = [
        item for item in (previous_messages or [])
        if isinstance(item, dict)
        and str(item.get("role") or "user").casefold() == "user"
        and not bool(item.get("sensitive"))
        and str(item.get("text") or "").strip()
    ]
    if not usable:
        return None
    usable.sort(key=lambda item: int(item.get("sequence_no") or 0), reverse=True)
    lifecycle_markers = (
        "任务", "待办", "完成", "做完", "继续", "处理", "评测", "开发", "修改", "修复", "实现", "准备", "计划",
    )
    antecedents: list[MemoryCandidate] = []
    for item in usable[:3]:
        source = str(item.get("text") or "").strip()
        if re.search(r"(?:整理学习计划|讨论了其他任务)", source):
            continue
        if not any(marker in source for marker in lifecycle_markers):
            continue
        source_core = source.strip(" ：:，,。！？!?；;")
        topic = re.sub(
            r"(?:还在|正在|继续|已经|已|等待|阻塞|暂停|处理|完成|做完|了)+$",
            "",
            source_core,
        ).strip(" ：:，,。！？!?；;")
        topic = topic or source
        entity = "随心记" if "随心记" in topic else "用户"
        attribute = topic.replace(entity, "", 1).strip(" 的：:，,。！？!?；;") or topic
        antecedent = _candidate(
            f"{note_id}:antecedent",
            "task",
            source,
            importance=0.7,
            confidence=0.8,
            entities=_entities(source),
            reason="reference_antecedent_identity_only",
            task_status="todo",
            evidence_span=source,
            subject=entity,
            predicate=attribute,
            object_value=topic,
            scope={"scope": "global", "canonical_topic": topic, "operation": "维护"},
        )
        antecedents.append(antecedent)
    # A bare demonstrative must not choose one of several recent tasks based
    # solely on recency.  It is safer to leave this ambiguous update out than
    # to mutate an arbitrary task identity.
    return antecedents[0] if len(antecedents) == 1 else None


def _is_reference_only_task_text(text: str) -> bool:
    """Whether text is an ambiguous demonstrative rather than a task title."""
    compact = re.sub(r"[\s。！？!?；;，,]+", "", str(text or ""))
    return bool(re.fullmatch(r"(?:这个|那个|它|这件事|上面那个)(?:继续做?|继续|做下去|完成它|取消它|就按这个)(?:吧)?", compact))


def _resolve_reference_fallback(
    note_id: str,
    current_text: str,
    candidates: list[MemoryCandidate],
    previous_messages: list[dict[str, Any]] | None,
) -> list[MemoryCandidate]:
    """Attach recent task identity to a current pronoun-only candidate."""
    if not previous_messages:
        # A demonstrative inside a self-contained task title (for example
        # “这个项目继续做”) is not a failed cross-message resolution when no
        # admissible prior user message was supplied.
        normalized: list[MemoryCandidate] = []
        for candidate in candidates:
            scope = dict(candidate.scope or {})
            if scope.get("reference_status") == "unresolved":
                scope.update({"reference_status": "not_applicable", "antecedent_note_id": None, "antecedent_offset": None, "antecedent_evidence_span": None, "resolution_confidence": None})
                normalized.append(replace(candidate, scope=scope))
            else:
                normalized.append(candidate)
        return normalized
    if not _REFERENCE_SIGNAL_RE.search(current_text):
        return candidates
    antecedent = _reference_antecedent_task(note_id, previous_messages)
    if antecedent is None:
        if _is_reference_only_task_text(current_text):
            return [candidate for candidate in candidates if candidate.memory_type != "task"]
        return candidates
    current_tasks = [candidate for candidate in candidates if candidate.memory_type == "task"]
    if current_tasks:
        resolved: list[MemoryCandidate] = []
        for candidate in candidates:
            if candidate.memory_type != "task":
                resolved.append(candidate)
                continue
            scope = dict(candidate.scope or {})
            scope.update(
                {
                    "canonical_topic": antecedent.scope.get("canonical_topic"),
                    "operation": antecedent.scope.get("operation"),
                    "task_family_key": antecedent.scope.get("task_family_key"),
                    "reference_status": "resolved",
                    "antecedent_note_id": next(
                        (str(item.get("note_id")) for item in (previous_messages or []) if str(item.get("text") or "") == str(antecedent.evidence_span or "")),
                        None,
                    ),
                    "antecedent_offset": -1,
                    "antecedent_evidence_span": antecedent.evidence_span,
                    "resolution_confidence": 0.82,
                }
            )
            resolved.append(replace(
                candidate,
                subject=antecedent.subject,
                predicate=antecedent.predicate,
                object_value=antecedent.object_value,
                memory_key=antecedent.memory_key,
                scope=scope,
            ))
        return resolved
    return candidates


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


def _clause_index_for_evidence(text: str, evidence_span: str | None) -> int | None:
    evidence = str(evidence_span or "").strip()
    if not evidence:
        return None
    clauses = split_clauses(text)
    if len(clauses) <= 1:
        return None
    for clause in clauses:
        if evidence in clause.text or clause.text in evidence:
            return clause.index
    return None


def _assign_evidence_atom_ids(candidates: list[MemoryCandidate], text: str) -> list[MemoryCandidate]:
    """Assign stable atom ids from evidence order, including same-clause atoms.

    Clause indexes are useful for Hybrid coverage, but a clause can contain
    several independently mutable assertions (for example two preferences).
    Atom ids therefore follow the left-to-right evidence spans instead of
    blindly reusing one clause id.
    """
    def normalized_evidence(candidate: MemoryCandidate) -> str:
        evidence = str(candidate.evidence_span or "").strip()
        if candidate.memory_type == "preference":
            trimmed = re.sub(r"^(?:和|以及|、|及)\s*", "", evidence)
            if trimmed and trimmed in text:
                return trimmed
        return evidence

    spans = {
        normalized_evidence(candidate)
        for candidate in candidates
        if normalized_evidence(candidate) and normalized_evidence(candidate) in text
    }
    ordered = sorted(spans, key=lambda span: (text.find(span), -len(span), span))
    atom_by_span = {span: f"a{index + 1}" for index, span in enumerate(ordered)}
    assigned: list[MemoryCandidate] = []
    positive_markers = ("喜欢", "更喜欢", "偏好", "偏向", "习惯", "只学", "重点放在")
    negative_markers = ("不喜欢", "讨厌", "厌恶", "不爱", "不想", "不打算", "暂时不", "过敏", "不用")
    for candidate in candidates:
        evidence = normalized_evidence(candidate)
        atom_id = atom_by_span.get(evidence)
        scope = dict(candidate.scope or {})
        polarity = candidate.polarity
        if candidate.memory_type == "preference" and candidate.extractor_type != "llm" and polarity in {None, "unknown"} and evidence:
            start = text.find(evidence)
            prefix = text[:start] if start >= 0 else ""
            markers = [(prefix.rfind(marker), "negative") for marker in negative_markers]
            markers.extend((prefix.rfind(marker), "positive") for marker in positive_markers)
            position, inferred = max(markers, default=(-1, "unknown"))
            if position >= 0:
                polarity = inferred
                scope["polarity"] = inferred
        if not atom_id and polarity == candidate.polarity and evidence == str(candidate.evidence_span or "").strip():
            assigned.append(candidate)
            continue
        if atom_id:
            scope["atom_id"] = atom_id
        assigned.append(replace(candidate, scope=scope, polarity=polarity, evidence_span=evidence))
    return assigned


def _merge_uncovered_rule_candidates(
    model_candidates: list[MemoryCandidate],
    rule_candidates: list[MemoryCandidate],
) -> list[MemoryCandidate]:
    """Fill atom/type coverage gaps without blindly unioning both extractors."""
    def is_same_fact(model_candidate: MemoryCandidate, rule_candidate: MemoryCandidate) -> bool:
        """A model candidate covers a rule only with identical identity and evidence.

        A clause may contain multiple preferences (for example coffee and tea),
        so clause/type is deliberately not a coverage key.
        """
        if model_candidate.memory_type != rule_candidate.memory_type:
            return False
        if model_candidate.effective_memory_key != rule_candidate.effective_memory_key:
            return False
        model_evidence = re.sub(r"\s+", " ", str(model_candidate.evidence_span or "").strip())
        rule_evidence = re.sub(r"\s+", " ", str(rule_candidate.evidence_span or "").strip())
        return bool(
            model_evidence
            and rule_evidence
            and (model_evidence in rule_evidence or rule_evidence in model_evidence)
        )

    # When both extractors describe the same evidence, keep the LLM's richer
    # wording/key but backfill deterministic fields that the model omitted
    # (notably task blocker/progress/closure and polarity).
    enriched_models: list[MemoryCandidate] = []
    for model_candidate in model_candidates:
        matching_rule = next(
            (
                rule_candidate
                for rule_candidate in rule_candidates
                if is_same_fact(model_candidate, rule_candidate)
            ),
            None,
        )
        if matching_rule is None:
            enriched_models.append(model_candidate)
            continue
        merged_scope = dict(matching_rule.scope or {})
        merged_scope.update(model_candidate.scope or {})
        task_status = model_candidate.task_status or matching_rule.task_status
        polarity = (
            matching_rule.polarity
            if model_candidate.polarity in {None, "unknown"}
            else model_candidate.polarity
        )
        if not bool((model_candidate.scope or {}).get("scope_explicit")) and bool((matching_rule.scope or {}).get("scope_explicit")):
            merged_scope["scope"] = matching_rule.scope.get("scope")
            merged_scope["scope_explicit"] = True
            merged_scope["scope_source"] = matching_rule.scope.get("scope_source") or "rules"
        enriched_models.append(
            replace(
                model_candidate,
                task_status=task_status,
                polarity=polarity,
                scope=merged_scope,
            )
        )
    model_candidates = enriched_models
    additions = [
        replace(
            candidate,
            reason="hybrid_atom_coverage_repair",
            extraction_reason="hybrid_atom_coverage_repair",
        )
        for candidate in rule_candidates
        if not any(is_same_fact(model_candidate, candidate) for model_candidate in model_candidates)
    ]
    return _dedupe([*model_candidates, *additions])


def extract_candidates(
    note_id: str,
    text: str,
    classification: dict[str, Any] | None = None,
    *,
    previous_messages: list[dict[str, Any]] | None = None,
    allow_llm_failure_fallback: bool = True,
) -> list[MemoryCandidate]:
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
        rows = _resolve_reference_fallback(
            note_id,
            rule_text,
            extract_rule_candidates(note_id, rule_text, classification),
            previous_messages,
        )
        return _assign_evidence_atom_ids(rows, rule_text)

    global _LLM_CONNECTION_CIRCUIT_OPEN_UNTIL
    try:
        model_candidates = extract_llm_candidates(
            note_id,
            text,
            classification,
            hints=_rule_hints(note_id, text, classification) if mode == "hybrid" else None,
            previous_messages=previous_messages,
        )
    except Exception as exc:
        # Production keeps the deterministic Rules degradation path.  The
        # independent evaluator can opt out to measure actual LLM
        # availability/quality instead of silently scoring a fallback.
        if not allow_llm_failure_fallback:
            raise
        if "connection_error" in str(exc).casefold():
            # One unavailable provider must not make every inbox message wait
            # for the transport timeout.  Rules remain the deterministic
            # fallback while a later call re-probes the provider.
            _LLM_CONNECTION_CIRCUIT_OPEN_UNTIL = time.monotonic() + 60.0
        if "LLM extraction circuit open" not in str(exc):
            _log_llm_failure(note_id, exc, mode=mode)
        rows = [
            replace(candidate, reason="llm_failed_rule_fallback", extraction_reason="llm_failed_rule_fallback")
            for candidate in extract_rule_candidates(note_id, rule_text, classification)
        ]
        return _assign_evidence_atom_ids(_resolve_reference_fallback(note_id, rule_text, rows, previous_messages), rule_text)

    if mode == "llm":
        return _assign_evidence_atom_ids(
            _resolve_reference_fallback(note_id, rule_text, model_candidates, previous_messages),
            rule_text,
        )
    # Hybrid 只补 LLM 未覆盖的 atom/type，避免非空 LLM 吞掉规则已发现的独立事实，
    # 同时避免无条件 rules/LLM 并集制造同一 atom 的冲突类型。
    if model_candidates:
        merged = _merge_uncovered_rule_candidates(
            model_candidates,
            _extract_atomic_rule_candidates(note_id, rule_text),
        )
        return _assign_evidence_atom_ids(
            _resolve_reference_fallback(note_id, rule_text, merged, previous_messages),
            rule_text,
        )
    if not allow_llm_failure_fallback:
        # An empty response is an LLM decision in strict evaluation mode; do
        # not turn it into a Rules-only answer.  Non-empty LLM results still
        # receive normal Hybrid atom-coverage repair above.
        return []
    # 空模型响应不是语义决策；保留确定性准入 fallback，但不要把它和非空模型结果做并集。
    rows = [
        replace(candidate, reason="llm_empty_rule_fallback", extraction_reason="llm_empty_rule_fallback")
        for candidate in extract_rule_candidates(note_id, rule_text, classification)
    ]
    return _assign_evidence_atom_ids(_resolve_reference_fallback(note_id, rule_text, rows, previous_messages), rule_text)
