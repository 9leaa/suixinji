"""Task-level LLM routing policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ModelRole(str, Enum):
    FAST = "fast"
    BALANCED = "balanced"
    STRONG = "strong"


class LLMTask(str, Enum):
    NOTE_CLASSIFICATION = "note_classification"
    MEMORY_EXTRACTION = "memory_extraction"
    QUERY_ROUTING = "query_routing"
    QUERY_SYNTHESIS = "query_synthesis"
    QUERY_COMPLEX_REASONING = "query_complex_reasoning"
    SUMMARY_DRAFT = "summary_draft"
    SUMMARY_REVIEW = "summary_review"
    MEMORY_CONFLICT_ADVISORY = "memory_conflict_advisory"
    CONSOLIDATION_CLUSTER_REVIEW = "consolidation_cluster_review"


@dataclass(frozen=True)
class ModelRoute:
    task: LLMTask
    role: ModelRole
    reason: str
    allow_strong: bool = False
    fallback_role: ModelRole | None = None


DEFAULT_ROUTES: dict[LLMTask, ModelRoute] = {
    LLMTask.NOTE_CLASSIFICATION: ModelRoute(LLMTask.NOTE_CLASSIFICATION, ModelRole.FAST, "cheap_structured_note_classification"),
    LLMTask.MEMORY_EXTRACTION: ModelRoute(LLMTask.MEMORY_EXTRACTION, ModelRole.FAST, "cheap_memory_candidate_extraction"),
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
    if isinstance(value, LLMTask):
        return value
    if value is None:
        return None
    try:
        return LLMTask(str(value).strip().lower())
    except ValueError:
        return None


def coerce_role(value: ModelRole | str | None) -> ModelRole | None:
    if isinstance(value, ModelRole):
        return value
    if value is None:
        return None
    try:
        return ModelRole(str(value).strip().lower())
    except ValueError:
        return None
