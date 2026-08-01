"""文件作用：模型能力策略。

项目关系：本文件依赖 无直接本地模块依赖；被 `core.model_router`。
"""



from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ModelRole(str, Enum):
    """类功能：`ModelRole` 封装与“模型能力策略”相关的数据结构、状态或行为。
    继承关系：继承 `str`、`Enum`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    FAST = "fast"
    BALANCED = "balanced"
    STRONG = "strong"


class LLMTask(str, Enum):
    """类功能：`LLMTask` 封装与“模型能力策略”相关的数据结构、状态或行为。
    继承关系：继承 `str`、`Enum`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    NOTE_CLASSIFICATION = "note_classification"
    MEMORY_EXTRACTION = "memory_extraction"
    QUERY_INTENT = "query_intent"
    QUERY_ROUTING = "query_routing"
    QUERY_SYNTHESIS = "query_synthesis"
    QUERY_COMPLEX_REASONING = "query_complex_reasoning"
    SUMMARY_DRAFT = "summary_draft"
    SUMMARY_REVIEW = "summary_review"
    MEMORY_CONFLICT_ADVISORY = "memory_conflict_advisory"
    CONSOLIDATION_CLUSTER_REVIEW = "consolidation_cluster_review"


@dataclass(frozen=True)
class ModelRoute:
    """类功能：`ModelRoute` 封装与“模型能力策略”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    task: LLMTask
    role: ModelRole
    reason: str
    allow_strong: bool = False
    fallback_role: ModelRole | None = None


DEFAULT_ROUTES: dict[LLMTask, ModelRoute] = {
    LLMTask.NOTE_CLASSIFICATION: ModelRoute(LLMTask.NOTE_CLASSIFICATION, ModelRole.FAST, "cheap_structured_note_classification"),
    LLMTask.MEMORY_EXTRACTION: ModelRoute(LLMTask.MEMORY_EXTRACTION, ModelRole.FAST, "cheap_memory_candidate_extraction"),
    LLMTask.QUERY_INTENT: ModelRoute(LLMTask.QUERY_INTENT, ModelRole.FAST, "cheap_structured_query_intent"),
    LLMTask.QUERY_ROUTING: ModelRoute(LLMTask.QUERY_ROUTING, ModelRole.FAST, "cheap_query_tool_selection"),
    LLMTask.QUERY_SYNTHESIS: ModelRoute(LLMTask.QUERY_SYNTHESIS, ModelRole.BALANCED, "normal_answer_synthesis"),
    LLMTask.QUERY_COMPLEX_REASONING: ModelRoute(
        LLMTask.QUERY_COMPLEX_REASONING,
        ModelRole.STRONG,
        "complex_multistep_reasoning",
        allow_strong=True,
        fallback_role=ModelRole.BALANCED,
    ),
    LLMTask.SUMMARY_DRAFT: ModelRoute(LLMTask.SUMMARY_DRAFT, ModelRole.BALANCED, "normal_summary_draft"),
    LLMTask.SUMMARY_REVIEW: ModelRoute(
        LLMTask.SUMMARY_REVIEW,
        ModelRole.BALANCED,
        "normal_summary_review",
        fallback_role=ModelRole.BALANCED,
    ),
    LLMTask.MEMORY_CONFLICT_ADVISORY: ModelRoute(
        LLMTask.MEMORY_CONFLICT_ADVISORY,
        ModelRole.STRONG,
        "high_risk_memory_relation_advisory",
        allow_strong=True,
        fallback_role=ModelRole.BALANCED,
    ),
    LLMTask.CONSOLIDATION_CLUSTER_REVIEW: ModelRoute(
        LLMTask.CONSOLIDATION_CLUSTER_REVIEW,
        ModelRole.STRONG,
        "monthly_semantic_cluster_review",
        allow_strong=True,
        fallback_role=ModelRole.BALANCED,
    ),
}


def coerce_task(value: LLMTask | str | None) -> LLMTask | None:
    """函数功能：`coerce_task` 负责处理 coerce task，服务于本文件职责：模型能力策略。
    传参：
        value: 待转换、校验或计算的值，类型为 `LLMTask | str | None`。
    返回结果说明：
        返回 `LLMTask | None`；未命中或无需处理时可返回 `None`。
    """
    if isinstance(value, LLMTask):
        return value
    if value is None:
        return None
    try:
        return LLMTask(str(value).strip().lower())
    except ValueError:
        return None


def coerce_role(value: ModelRole | str | None) -> ModelRole | None:
    """函数功能：`coerce_role` 负责处理 coerce role，服务于本文件职责：模型能力策略。
    传参：
        value: 待转换、校验或计算的值，类型为 `ModelRole | str | None`。
    返回结果说明：
        返回 `ModelRole | None`；未命中或无需处理时可返回 `None`。
    """
    if isinstance(value, ModelRole):
        return value
    if value is None:
        return None
    try:
        return ModelRole(str(value).strip().lower())
    except ValueError:
        return None
