"""文件作用：Memory 领域模型与常量。

项目关系：本文件依赖 无直接本地模块依赖；被 `agent.hooks.context`、`eval.eval_memory`、`eval.eval_memory_quality`、`eval.large_live_retrieval_eval` 等 45 个模块。
"""



from __future__ import annotations

import re
import uuid
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

MEMORY_TYPES = {"episodic", "semantic", "preference", "task"}
MEMORY_STATUSES = {
    "active",
    "superseded",
    "conflicted",
    "forgotten",
    "archived",
    "pending_review",
    # 为兼容现有公开命令而保留。
    "deleted",
    "expired",
}
# “正在进行”是任务的自然语言进展描述，不再是独立的持久化状态；统一归入 todo。
TASK_STATUSES = {"todo", "blocked", "done", "cancelled"}
SOURCE_RELATIONS = {"created_from", "supported_by", "updated_by", "contradicted_by", "summarized_from"}
DECISION_RELATIONS = {"new", "same", "merge", "update_task", "supersede", "conflict"}
DECISION_ACTIONS = {"insert", "add_source", "merge", "update_task", "supersede", "conflict", "pending_review", "discard"}
MEMORY_RELATION_TYPES = {"supersedes", "superseded_by", "conflicts_with", "supports", "summarized_from", "derived_from"}
MEMORY_EXTRACTION_STATUSES = {"pending", "processing", "completed", "empty", "partial", "failed"}
MEMORY_CONSOLIDATION_STATUSES = {"running", "completed", "failed"}
MEMORY_KEY_VERSION = "memory-key-v2"
MEMORY_KEY_V3_VERSION = "memory-key-v3"
SLOT_SEMANTIC_PREDICATES = {
    "location",
    "current_project",
    "currentproject",
    "current_employer",
    "currentemployer",
    "learning_focus",
    "learningfocus",
    "birthplace",
}


def utc_now_iso() -> str:
    """函数功能：`utc_now_iso` 负责处理 UTC 时间 now iso，服务于本文件职责：Memory 领域模型与常量。
    传参：
        无。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    """函数功能：`new_id` 负责处理 new id，服务于本文件职责：Memory 领域模型与常量。
    传参：
        prefix: prefix 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def candidate_id_for(note_id: str, memory_type: str, content: str) -> str:
    """函数功能：`candidate_id_for` 负责处理 candidate id for，服务于本文件职责：Memory 领域模型与常量。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        memory_type: memory type 参数，由调用方传入，类型为 `str`。
        content: 需要处理、保存或展示的文本内容，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    key = f"{note_id}\x1f{memory_type}\x1f{normalize_content(content)}"
    return f"cand_{uuid.uuid5(uuid.NAMESPACE_URL, key).hex[:16]}"


def candidate_id_for_evidence(
    note_id: str,
    memory_type: str,
    content: str,
    *,
    memory_key: str | None = None,
    evidence_span: str | None = None,
    clause_index: int | None = None,
) -> str:
    """函数功能：`candidate_id_for_evidence` 负责处理 candidate id for evidence，服务于本文件职责：Memory 领域模型与常量。
    传参：
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        memory_type: memory type 参数，由调用方传入，类型为 `str`。
        content: 需要处理、保存或展示的文本内容，类型为 `str`。
        memory_key: memory key 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        evidence_span: evidence span 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        clause_index: clause index 参数，由调用方传入，类型为 `int | None`，默认值为 `None`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    key = "\x1f".join(
        [
            str(note_id or ""),
            str(clause_index if clause_index is not None else ""),
            str(memory_type or ""),
            str(memory_key or ""),
            normalize_content(evidence_span or content),
        ]
    )
    return f"cand_{uuid.uuid5(uuid.NAMESPACE_URL, key).hex[:16]}"


def normalize_content(text: str) -> str:
    """函数功能：`normalize_content` 负责归一化 content，服务于本文件职责：Memory 领域模型与常量。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    value = str(text or "").casefold()
    value = re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE)
    for token in ("用户", "我现在", "我最近", "我", "本人", "目前", "现在", "最近"):
        value = value.replace(token, "")
    return value


def memory_key_for(
    memory_type: str,
    *,
    subject: str | None = None,
    predicate: str | None = None,
    object_value: str | None = None,
    content: str = "",
) -> str:
    """函数功能：`memory_key_for` 负责处理 memory key for，服务于本文件职责：Memory 领域模型与常量。
    传参：
        memory_type: memory type 参数，由调用方传入，类型为 `str`。
        subject: subject 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        predicate: predicate 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        object_value: object value 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        content: 需要处理、保存或展示的文本内容，类型为 `str`，默认值为 `''`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    subject_key = normalize_content(subject or "用户") or "用户"
    if subject_key in {"user", "me", "myself"}:
        subject_key = "user"
    predicate_key = normalize_content(predicate or memory_type) or memory_type
    object_key = normalize_content(object_value or "")
    if memory_type == "preference":
        # 这里刻意排除 polarity：同一主题的正向和负向陈述必须进入同一个裁决 key。
        topic = object_key or normalize_content(content)
        topic = re.sub(
            r"(?:用户|本人|我现在|我最近|我|喜欢|更喜欢|最喜欢|偏好|习惯|不喜欢|讨厌|厌恶|不爱|不想|不打算|暂时不|过敏|优先选择|优先)",
            "",
            topic,
        )
        topic = re.sub(r"^(?:喝|吃|用|使用|采用|选择|选|穿|看|听|玩|住|做|学习|学|买|去)", "", topic)
        return f"preference:{subject_key}:{topic or 'unspecified'}:global"
    if memory_type == "task":
        task_text = object_key or normalize_content(content)
        task_text = re.sub(
            r"(?:记得|需要|待办|完成|完善|处理|修复|修改|修|改|实现|已经|正在|进行中|开始|继续|阻塞|卡住|取消|不用做|不做了|现在|可以|还要给|补充测试|遇到)",
            "",
            task_text,
        ).lstrip("是").rstrip("了")
        return f"task:{subject_key}:{predicate_key}:{task_text or 'unspecified'}"
    if memory_type == "semantic":
        if predicate_key in SLOT_SEMANTIC_PREDICATES:
            return f"semantic:{subject_key}:{predicate_key}"
        topic = object_key or normalize_content(content)
        if not topic:
            digest = hashlib.sha1(f"{subject_key}\x1f{predicate_key}\x1f{normalize_content(content)}".encode("utf-8")).hexdigest()
            topic = digest[:16]
        return f"semantic:{subject_key}:{predicate_key}:{topic[:96]}"
    return f"{memory_type}:{subject_key}:{predicate_key}:{object_key or normalize_content(content)}"


@dataclass(frozen=True)
class MemoryCandidate:
    """类功能：`MemoryCandidate` 封装与“Memory 领域模型与常量”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    memory_type: str
    content: str
    importance: float
    confidence: float
    entities: list[str] = field(default_factory=list)
    should_store: bool = True
    task_status: str | None = None
    reason: str | None = None
    candidate_id: str = field(default_factory=lambda: new_id("cand"))
    note_id: str | None = None
    space_id: str | None = None
    subject: str | None = None
    predicate: str | None = None
    object_value: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    evidence_span: str | None = None
    clause_index: int | None = None
    extraction_reason: str | None = None
    memory_key: str | None = None
    polarity: str | None = None
    scope: dict[str, Any] = field(default_factory=dict)
    extractor_type: str = "rules"
    extractor_version: str = "memory-extractor-v1"
    model: str | None = None
    prompt_hash: str | None = None
    memory_key_version: str = MEMORY_KEY_VERSION

    def __post_init__(self) -> None:
        """函数功能：`MemoryCandidate.__post_init__` 在类 `MemoryCandidate` 中负责处理 post init，服务于本文件职责：Memory 领域模型与常量。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        if self.memory_type not in MEMORY_TYPES:
            raise ValueError(f"invalid memory_type: {self.memory_type}")
        if self.task_status is not None and self.task_status not in TASK_STATUSES:
            raise ValueError(f"invalid task_status: {self.task_status}")

    @property
    def normalized_content(self) -> str:
        """函数功能：`MemoryCandidate.normalized_content` 在类 `MemoryCandidate` 中负责处理 normalized content，服务于本文件职责：Memory 领域模型与常量。
        传参：
            无。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return normalize_content(self.content)

    @property
    def effective_reason(self) -> str | None:
        """函数功能：`MemoryCandidate.effective_reason` 在类 `MemoryCandidate` 中负责处理 effective reason，服务于本文件职责：Memory 领域模型与常量。
        传参：
            无。
        返回结果说明：
            返回 `str | None`；未命中或无需处理时可返回 `None`。
        """
        return self.extraction_reason or self.reason

    @property
    def effective_memory_key(self) -> str:
        """函数功能：`MemoryCandidate.effective_memory_key` 在类 `MemoryCandidate` 中负责处理 effective memory key，服务于本文件职责：Memory 领域模型与常量。
        传参：
            无。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return self.memory_key or memory_key_for(
            self.memory_type,
            subject=self.subject,
            predicate=self.predicate,
            object_value=self.object_value,
            content=self.content,
        )


@dataclass(frozen=True, kw_only=True)
class MemoryDecision:
    """类功能：`MemoryDecision` 封装与“Memory 领域模型与常量”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    candidate_id: str
    relation: str
    target_memory_ids: list[str]
    confidence: float
    reason: str
    evidence: list[str]
    recommended_action: str
    decision_id: str = field(default_factory=lambda: new_id("decision"))
    policy_version: str = "memory-policy-v1"
    adjudicator_version: str = "memory-adjudicator-v1"
    model: str | None = None
    prompt_hash: str | None = None
    input_hash: str | None = None
    target_snapshot_version: int | None = None
    retry_of_decision_id: str | None = None

    def __post_init__(self) -> None:
        """函数功能：`MemoryDecision.__post_init__` 在类 `MemoryDecision` 中负责处理 post init，服务于本文件职责：Memory 领域模型与常量。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        if self.relation not in DECISION_RELATIONS:
            raise ValueError(f"invalid decision relation: {self.relation}")
        if self.recommended_action not in DECISION_ACTIONS:
            raise ValueError(f"invalid decision action: {self.recommended_action}")
        if not 0 <= float(self.confidence) <= 1:
            raise ValueError("decision confidence must be between 0 and 1")


@dataclass(frozen=True)
class MemorySource:
    """类功能：`MemorySource` 封装与“Memory 领域模型与常量”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    memory_id: str
    note_id: str
    relation: str
    created_at: str


@dataclass(frozen=True)
class MemoryVersion:
    """类功能：`MemoryVersion` 封装与“Memory 领域模型与常量”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    id: str
    memory_id: str
    version: int
    content: str
    status: str
    reason: str | None
    source_note_id: str | None
    created_at: str
    task_status: str | None = None
    confidence: float | None = None
    importance: float | None = None
    valid_from: str | None = None
    valid_until: str | None = None


@dataclass(frozen=True)
class MemoryRelation:
    """类功能：`MemoryRelation` 封装与“Memory 领域模型与常量”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    id: str
    space_id: str
    source_memory_id: str
    target_memory_id: str
    relation: str
    decision_id: str | None
    created_at: str


@dataclass(frozen=True)
class MemoryExtractionState:
    """类功能：`MemoryExtractionState` 封装与“Memory 领域模型与常量”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    note_id: str
    space_id: str
    status: str
    candidate_count: int
    processed_count: int
    attempt_count: int
    last_error: str | None
    started_at: str | None
    completed_at: str | None
    updated_at: str


@dataclass(frozen=True)
class ConsolidationRun:
    """类功能：`ConsolidationRun` 封装与“Memory 领域模型与常量”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    id: str
    space_id: str
    cadence: str
    period_key: str
    status: str
    started_at: str
    completed_at: str | None
    error: str | None
    result_json: str | None


@dataclass(frozen=True)
class MemoryRecord:
    """类功能：`MemoryRecord` 封装与“Memory 领域模型与常量”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    id: str
    space_id: str
    memory_type: str
    content: str
    normalized_content: str
    importance: float
    confidence: float
    status: str
    valid_from: str | None
    valid_until: str | None
    created_at: str
    updated_at: str
    last_accessed_at: str | None
    access_count: int
    current_version: int
    task_status: str | None = None
    sources: list[MemorySource] = field(default_factory=list)
    versions: list[MemoryVersion] = field(default_factory=list)
    subject: str | None = None
    predicate: str | None = None
    object_value: str | None = None
    last_confirmed_at: str | None = None
    memory_key: str | None = None
    polarity: str | None = None
    scope: dict[str, Any] = field(default_factory=dict)
    memory_key_version: str = MEMORY_KEY_VERSION

    @property
    def effective_memory_key(self) -> str:
        """函数功能：`MemoryRecord.effective_memory_key` 在类 `MemoryRecord` 中负责处理 effective memory key，服务于本文件职责：Memory 领域模型与常量。
        传参：
            无。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return self.memory_key or memory_key_for(
            self.memory_type,
            subject=self.subject,
            predicate=self.predicate,
            object_value=self.object_value,
            content=self.content,
        )

    def to_dict(self) -> dict[str, Any]:
        """函数功能：`MemoryRecord.to_dict` 在类 `MemoryRecord` 中负责转换为目标结构 dict，服务于本文件职责：Memory 领域模型与常量。
        传参：
            无。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
        return {
            "id": self.id,
            "space_id": self.space_id,
            "memory_type": self.memory_type,
            "content": self.content,
            "normalized_content": self.normalized_content,
            "importance": self.importance,
            "confidence": self.confidence,
            "status": self.status,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_accessed_at": self.last_accessed_at,
            "access_count": self.access_count,
            "current_version": self.current_version,
            "task_status": self.task_status,
            "subject": self.subject,
            "predicate": self.predicate,
            "object_value": self.object_value,
            "last_confirmed_at": self.last_confirmed_at,
            "memory_key": self.memory_key,
            "memory_key_version": self.memory_key_version,
            "polarity": self.polarity,
            "scope": self.scope,
            "sources": [source.__dict__ for source in self.sources],
            "versions": [version.__dict__ for version in self.versions],
        }
