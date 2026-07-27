"""Bounded deterministic query planning for complex retrieval.

The planner is deliberately conservative: it detects query shape and creates
small, traceable retrieval variants. It never changes Memory/Note state and
does not treat a similarity score as an update decision. An optional LLM
planner can be layered on top later; this module remains the safe fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

from agent.query_route_features import classify_structural_route, extract_route_features
from core import settings


_FILLERS = ("请问", "帮我", "告诉我", "查一下", "看一下", "相关内容", "相关记录")
_BOUNDARY_STRIP = " ，,。！？?!；;\n"


def _max_variants() -> int:
    configured = max(2, int(getattr(settings, "QUERY_MAX_TOTAL_QUERIES", 5)))
    return max(1, min(4, configured - 1))


@dataclass(frozen=True)
class QueryPlan:
    complexity: str
    rewritten_query: str
    retrieval_queries: tuple[str, ...]
    use_query_rewrite: bool
    use_decomposition: bool
    use_step_back: bool
    routing_state: str = "simple"
    routing_confidence: float = 1.0
    routing_reasons: tuple[str, ...] = ()


def _rewrite(question: str) -> str:
    value = " ".join(str(question or "").split()).strip()
    for filler in _FILLERS:
        value = value.replace(filler, " ")
    value = re.sub(r"\s+", " ", value).strip(_BOUNDARY_STRIP)
    return value or str(question or "").strip()


def _decompose(question: str, *, features: Any | None = None) -> list[str]:
    """Return bounded, independently searchable query fragments."""
    value = str(question or "").strip()
    features = features or extract_route_features(value)
    parts = [part.strip(_BOUNDARY_STRIP) for part in features.clauses if part.strip(_BOUNDARY_STRIP)]

    compare = re.search(
        r"(?:比较|对比)\s*([^，,。；;？?]{2,40}?)\s*(?:和|与|及)\s*([^，,。；;？?的]{2,40})",
        value,
    )
    if compare:
        parts.extend((compare.group(1).strip(), compare.group(2).strip()))

    english_compare = re.search(
        r"compare\s+(.+?)\s+(?:and|vs\.?|versus)\s+(.+?)(?:\s+and\s+|\s+which\b|$)",
        value,
        flags=re.IGNORECASE,
    )
    if english_compare:
        parts.extend((english_compare.group(1).strip(), english_compare.group(2).strip()))

    if getattr(features, "has_relationship_request", False) and len(getattr(features, "entity_candidates", ())) >= 2:
        parts.extend(features.entity_candidates[:2])

    return list(dict.fromkeys(part for part in parts if len(part) >= 2))[: max(1, int(getattr(settings, "QUERY_MAX_SUBQUESTIONS", 3)))]


def _step_back(question: str, *, features: Any | None = None) -> str | None:
    features = features or extract_route_features(question)
    value = str(question or "").strip()
    lowered = value.casefold()
    if "step-back" in lowered or "step back" in lowered:
        return value + " 的上位概念、背景与约束"
    if features.has_causal_request:
        return value + " 的背景与原因"
    if features.has_trend_request:
        return value + " 的历史变化与趋势"
    if features.has_comparison:
        return value + " 的共同点、差异与适用场景"
    if features.has_relationship_request or features.has_summary_request:
        return value + " 的证据范围、背景与约束"
    if "怎么" in value or "如何" in value or "how" in lowered:
        return value + " 的方法、步骤与注意事项"
    return None


def _merge_model_plan(base: QueryPlan, model_plan: Mapping[str, Any] | None) -> QueryPlan:
    if not model_plan:
        return base

    model_complexity = str(model_plan.get("complexity") or "uncertain").strip().lower()
    strategies = {str(item).strip().lower() for item in (model_plan.get("strategies") or []) if str(item).strip()}
    rewrites = [str(item).strip() for item in (model_plan.get("rewritten_queries") or []) if str(item).strip()]
    rewrites = rewrites[: max(0, int(getattr(settings, "QUERY_MAX_REWRITES", 2)))]
    subqueries: list[str] = []
    for item in model_plan.get("sub_questions") or []:
        if isinstance(item, Mapping):
            value = item.get("query")
        else:
            value = item
        if value is not None and str(value).strip():
            subqueries.append(str(value).strip())
    step_back = str(model_plan.get("step_back_query") or "").strip() or None

    # A model may clarify an uncertain route, but it may not downgrade a
    # deterministic explicit-complex decision. The local guard remains the
    # authority when the model output is incomplete.
    if base.routing_state == "complex":
        complexity = "complex"
    elif model_complexity in {"simple", "complex"}:
        complexity = model_complexity
    else:
        complexity = base.complexity
    if complexity == "simple":
        return QueryPlan(
            complexity="simple",
            rewritten_query=base.rewritten_query,
            retrieval_queries=(),
            use_query_rewrite=False,
            use_decomposition=False,
            use_step_back=False,
            routing_state="simple",
            routing_confidence=base.routing_confidence,
            routing_reasons=tuple(dict.fromkeys((*base.routing_reasons, "llm_plan_simple"))),
        )

    use_query_rewrite = base.use_query_rewrite or "rewrite" in strategies or bool(rewrites)
    use_decomposition = base.use_decomposition or ("decomposition" in strategies and len(subqueries) >= 2)
    use_step_back = base.use_step_back or ("step_back" in strategies and step_back is not None)
    variants: list[str] = []
    if use_query_rewrite:
        variants.extend(rewrites)
    if use_decomposition:
        variants.extend(subqueries)
    if use_step_back and step_back:
        variants.append(step_back)
    variants = list(dict.fromkeys(item for item in variants if item != base.rewritten_query))[:_max_variants()]
    resolved_state = base.routing_state if model_complexity == "uncertain" else "complex"
    return QueryPlan(
        complexity="complex",
        rewritten_query=base.rewritten_query,
        retrieval_queries=tuple(variants or base.retrieval_queries),
        use_query_rewrite=use_query_rewrite,
        use_decomposition=use_decomposition,
        use_step_back=use_step_back,
        routing_state=resolved_state,
        routing_confidence=base.routing_confidence,
        routing_reasons=tuple(dict.fromkeys((*base.routing_reasons, "llm_plan"))),
    )


def build_query_plan(question: str, *, model_plan: Mapping[str, Any] | None = None) -> QueryPlan:
    original = " ".join(str(question or "").split()).strip()
    rewritten = _rewrite(original)
    features = extract_route_features(original)
    decision = classify_structural_route(features)

    # Uncertain results are routed through the reasoning path until a model
    # second opinion is available. This avoids silently treating a long or
    # referential query as a one-hop lookup.
    is_complex_route = decision.complexity != "simple"
    parts = _decompose(rewritten, features=features) if is_complex_route else []
    step_back = _step_back(rewritten, features=features) if is_complex_route else None

    use_query_rewrite = is_complex_route and (
        rewritten != original or "rewrite" in decision.suggested_strategies
    )
    use_decomposition = is_complex_route and "decomposition" in decision.suggested_strategies and len(parts) >= 2
    use_step_back = is_complex_route and "step_back" in decision.suggested_strategies and step_back is not None

    variants: list[str] = []
    if use_query_rewrite and rewritten and rewritten != original:
        variants.append(rewritten)
    if use_decomposition:
        variants.extend(part for part in parts if part not in variants and part != rewritten)
    if use_step_back and step_back and step_back not in variants and step_back != rewritten:
        variants.append(step_back)
    variants = list(dict.fromkeys(variants))[:_max_variants()]

    base = QueryPlan(
        complexity="complex" if is_complex_route else "simple",
        rewritten_query=rewritten,
        retrieval_queries=tuple(variants),
        use_query_rewrite=use_query_rewrite,
        use_decomposition=use_decomposition,
        use_step_back=use_step_back,
        routing_state=decision.complexity,
        routing_confidence=decision.confidence,
        routing_reasons=decision.reasons,
    )
    return _merge_model_plan(base, model_plan)
