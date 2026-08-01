"""文件作用：模型路由。

项目关系：本文件依赖 `core`、`core.model_policy`；被 `core.llm_client`、`memory.extractor`、`tests.test_stage7_model_routing_and_clause_extraction`。
"""



from __future__ import annotations

from dataclasses import replace

from core import settings
from core.model_policy import DEFAULT_ROUTES, LLMTask, ModelRole, ModelRoute, coerce_role, coerce_task


def route_model(
    *,
    task: LLMTask | str | None = None,
    model_role: ModelRole | str | None = None,
    range_key: str | None = None,
    strong_hint: bool = False,
) -> ModelRoute:
    """函数功能：`route_model` 负责路由 model，服务于本文件职责：模型路由。
    传参：
        task: task 参数，由调用方传入，类型为 `LLMTask | str | None`，默认值为 `None`。
        model_role: model role 参数，由调用方传入，类型为 `ModelRole | str | None`，默认值为 `None`。
        range_key: range key 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        strong_hint: strong hint 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
    返回结果说明：
        返回 `ModelRoute` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    explicit_role = coerce_role(model_role)
    resolved_task = coerce_task(task)

    if not settings.MODEL_ROUTING_ENABLED:
        return ModelRoute(resolved_task or LLMTask.QUERY_SYNTHESIS, explicit_role or ModelRole.BALANCED, "model_routing_disabled")

    if resolved_task is None:
        return ModelRoute(LLMTask.QUERY_SYNTHESIS, explicit_role or ModelRole.BALANCED, "legacy_model_role")

    route = DEFAULT_ROUTES[resolved_task]
    if resolved_task == LLMTask.SUMMARY_REVIEW and str(range_key or "").lower() in {"month", "monthly", "half_year", "year", "yearly"}:
        route = replace(route, role=ModelRole.STRONG, allow_strong=True, reason="long_range_summary_review", fallback_role=ModelRole.BALANCED)
    if explicit_role is not None:
        route = replace(route, role=explicit_role, reason=f"explicit_role:{route.reason}")
    if route.role == ModelRole.STRONG and not (settings.STRONG_ESCALATION_ENABLED or strong_hint):
        return replace(route, role=route.fallback_role or ModelRole.BALANCED, reason=f"strong_disabled:{route.reason}")
    return route
