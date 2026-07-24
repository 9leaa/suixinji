"""Resolve LLM model roles from task names."""

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
    """Return the model route for one LLM call.

    Existing callers can keep passing ``model_role``. New callers should pass a
    task name so routing policy stays centralized.
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
