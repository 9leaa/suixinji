"""文件作用：复杂度和子句特征提取。

项目关系：本文件依赖 `agent.query_planner`、`agent.query_route_features`、`core`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from __future__ import annotations

from agent.query_planner import build_query_plan
from agent.query_route_features import (
    extract_route_features,
    should_call_query_intent_llm,
    structural_route,
)


def test_simple_single_topic_keeps_fast_path_without_variants():
    """函数功能：`test_simple_single_topic_keeps_fast_path_without_variants` 负责验证 simple single topic keeps fast path without variants 场景，服务于本文件职责：复杂度和子句特征提取。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    plan = build_query_plan("请帮我查一下RAG混合检索")
    assert plan.complexity == "simple"
    assert plan.routing_state == "simple"
    assert plan.retrieval_queries == ()
    assert not plan.use_query_rewrite
    assert not should_call_query_intent_llm("请帮我查一下RAG混合检索")


def test_negated_complex_markers_do_not_upgrade_simple_query():
    """函数功能：`test_negated_complex_markers_do_not_upgrade_simple_query` 负责验证 negated complex markers do not upgrade simple query 场景，服务于本文件职责：复杂度和子句特征提取。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    features, decision = structural_route("只查询RAG混合检索，不要比较其它主题，也不要分析原因")
    assert features.negated_operations
    assert decision.complexity == "simple"


def test_multi_clause_connectors_trigger_decomposition():
    """函数功能：`test_multi_clause_connectors_trigger_decomposition` 负责验证 multi clause connectors trigger decomposition 场景，服务于本文件职责：复杂度和子句特征提取。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    plan = build_query_plan("RAG混合检索现在什么状态，并且SQL索引是否完成，另外说明Agent简历的偏好")
    assert plan.complexity == "complex"
    assert plan.use_decomposition
    assert len(plan.retrieval_queries) >= 2


def test_question_mark_multi_hop_is_split():
    """函数功能：`test_question_mark_multi_hop_is_split` 负责验证 question mark multi hop is split 场景，服务于本文件职责：复杂度和子句特征提取。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    plan = build_query_plan("查RAG混合检索？再查SQL索引？最后把两条结果合并成一个结论")
    assert plan.complexity == "complex"
    assert plan.use_decomposition
    assert len(plan.retrieval_queries) >= 2


def test_chinese_full_stop_splits_independent_questions(monkeypatch):
    """函数功能：`test_chinese_full_stop_splits_independent_questions` 负责验证 chinese full stop splits independent questions 场景，服务于本文件职责：复杂度和子句特征提取。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    from core import settings

    monkeypatch.setattr(settings, "QUERY_MAX_SUBQUESTIONS", 4)
    plan = build_query_plan("我喜欢喝什么？我找什么工作？我什么时候去的植物园。我喜欢喝咖啡吗？")

    assert plan.retrieval_queries == (
        "我喜欢喝什么",
        "我找什么工作",
        "我什么时候去的植物园",
        "我喜欢喝咖啡吗",
    )


def test_english_compare_is_complex_and_decomposed():
    """函数功能：`test_english_compare_is_complex_and_decomposed` 负责验证 english compare is complex and decomposed 场景，服务于本文件职责：复杂度和子句特征提取。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    plan = build_query_plan("Compare Canonical Key and SQL indexes and explain which is more suitable")
    assert plan.complexity == "complex"
    assert plan.use_decomposition
    assert any("Canonical Key" in item for item in plan.retrieval_queries)


def test_anaphora_requires_llm_second_opinion():
    """函数功能：`test_anaphora_requires_llm_second_opinion` 负责验证 anaphora requires llm second opinion 场景，服务于本文件职责：复杂度和子句特征提取。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    assert should_call_query_intent_llm("那件事呢？")
    features = extract_route_features("那件事呢？")
    assert features.has_anaphora


def test_long_single_scope_query_stays_simple():
    """函数功能：`test_long_single_scope_query_stays_simple` 负责验证 long single scope query stays simple 场景，服务于本文件职责：复杂度和子句特征提取。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    features, decision = structural_route(
        "请从长期记忆中只查找RAG这一项主题的唯一当前结论并返回对应的一条记录，不要扩展到其他主题，也不需要额外解释背景和上下文"
    )
    assert len(features.normalized_query) > 50
    assert decision.complexity == "simple"
    assert not should_call_query_intent_llm(features.normalized_query)


def test_causal_query_gets_step_back():
    """函数功能：`test_causal_query_gets_step_back` 负责验证 causal query gets step back 场景，服务于本文件职责：复杂度和子句特征提取。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    plan = build_query_plan("为什么RAG学习进度发生变化，结合之前记录解释原因")
    assert plan.complexity == "complex"
    assert plan.use_step_back
    assert any(item.endswith("背景与原因") for item in plan.retrieval_queries)


def test_trace_fields_are_bounded_and_explainable():
    """函数功能：`test_trace_fields_are_bounded_and_explainable` 负责验证 trace fields are bounded and explainable 场景，服务于本文件职责：复杂度和子句特征提取。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    plan = build_query_plan("比较Canonical Key和SQL索引的当前结论")
    assert 0 <= plan.routing_confidence <= 1
    assert plan.routing_reasons
    assert len(plan.retrieval_queries) <= 4


def test_validated_model_plan_adds_bounded_subquestions():
    """函数功能：`test_validated_model_plan_adds_bounded_subquestions` 负责验证 validated model plan adds bounded subquestions 场景，服务于本文件职责：复杂度和子句特征提取。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    plan = build_query_plan(
        "那件事呢？",
        model_plan={
            "complexity": "complex",
            "strategies": ["decomposition", "step_back"],
            "sub_questions": [{"query": "当前任务状态"}, {"query": "最近相关记录"}],
            "step_back_query": "需要哪些背景证据",
        },
    )
    assert plan.complexity == "complex"
    assert plan.use_decomposition
    assert plan.use_step_back
    assert len(plan.retrieval_queries) <= 4


def test_simple_model_plan_cannot_emit_variants():
    """函数功能：`test_simple_model_plan_cannot_emit_variants` 负责验证 simple model plan cannot emit variants 场景，服务于本文件职责：复杂度和子句特征提取。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    plan = build_query_plan(
        "那件事呢？",
        model_plan={
            "complexity": "simple",
            "strategies": ["none"],
            "rewritten_queries": ["invented extra query"],
        },
    )
    assert plan.complexity == "simple"
    assert plan.retrieval_queries == ()
