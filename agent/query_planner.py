"""文件作用：查询计划。

项目关系：本文件依赖 `agent.query_route_features`、`core`；被 `agent.query_agent`、`eval.large_live_retrieval_eval`、`eval.p4_query_routing_eval`、`eval.resume_memory_system_benchmark` 等 8 个模块。
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
    """函数功能：`_max_variants` 负责处理 max variants，服务于本文件职责：查询计划。
    传参：
        无。
    返回结果说明：
        返回 `int`，表示计算得到的数值结果。
    """
    configured = max(2, int(getattr(settings, "QUERY_MAX_TOTAL_QUERIES", 5)))
    return max(1, min(4, configured - 1))


@dataclass(frozen=True)
class QueryPlan:
    """类功能：`QueryPlan` 封装与“查询计划”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
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
    """函数功能：`_rewrite` 负责重写，服务于本文件职责：查询计划。
    传参：
        question: 用户问题文本，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    value = " ".join(str(question or "").split()).strip()
    for filler in _FILLERS:
        value = value.replace(filler, " ")
    value = re.sub(r"\s+", " ", value).strip(_BOUNDARY_STRIP)
    return value or str(question or "").strip()


def _decompose(question: str, *, features: Any | None = None) -> list[str]:
    """函数功能：`_decompose` 负责处理 decompose，服务于本文件职责：查询计划。
    传参：
        question: 用户问题文本，类型为 `str`。
        features: features 参数，由调用方传入，类型为 `Any | None`，默认值为 `None`。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
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
    """函数功能：`_step_back` 负责处理 step back，服务于本文件职责：查询计划。
    传参：
        question: 用户问题文本，类型为 `str`。
        features: features 参数，由调用方传入，类型为 `Any | None`，默认值为 `None`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
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
    """函数功能：`_merge_model_plan` 负责合并 model plan，服务于本文件职责：查询计划。
    传参：
        base: base 参数，由调用方传入，类型为 `QueryPlan`。
        model_plan: model plan 参数，由调用方传入，类型为 `Mapping[str, Any] | None`。
    返回结果说明：
        返回 `QueryPlan` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
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

    # 模型可以澄清不确定路由，但不能降级确定性的显式复杂判断；模型输出不完整时，本地 guard 仍是权威。
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
    # 多子句问题先为每个子句保留一个检索槽位，再把有限槽位用于全局改写。
    if use_decomposition:
        variants.extend(subqueries)
    if use_query_rewrite:
        variants.extend(rewrites)
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
    """函数功能：`build_query_plan` 负责构建 query plan，服务于本文件职责：查询计划。
    传参：
        question: 用户问题文本，类型为 `str`。
        model_plan: model plan 参数，由调用方传入，类型为 `Mapping[str, Any] | None`，默认值为 `None`。
    返回结果说明：
        返回 `QueryPlan` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    original = " ".join(str(question or "").split()).strip()
    rewritten = _rewrite(original)
    features = extract_route_features(original)
    decision = classify_structural_route(features)

    # 不确定结果先进入推理路径，直到模型二次判断可用；避免把长问题或指代问题静默当成单跳查询。
    is_complex_route = decision.complexity != "simple"
    parts = _decompose(rewritten, features=features) if is_complex_route else []
    step_back = _step_back(rewritten, features=features) if is_complex_route else None

    use_query_rewrite = is_complex_route and (
        rewritten != original or "rewrite" in decision.suggested_strategies
    )
    use_decomposition = is_complex_route and "decomposition" in decision.suggested_strategies and len(parts) >= 2
    use_step_back = is_complex_route and "step_back" in decision.suggested_strategies and step_back is not None

    variants: list[str] = []
    # 子句覆盖优先于全局改写；总查询预算较小，如果改写排在前面，四子句请求的最后一问可能被静默丢弃。
    if use_decomposition:
        variants.extend(part for part in parts if part not in variants and part != rewritten)
    if use_query_rewrite and rewritten and rewritten != original:
        variants.append(rewritten)
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
