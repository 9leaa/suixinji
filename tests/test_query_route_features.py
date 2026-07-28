from __future__ import annotations

from agent.query_planner import build_query_plan
from agent.query_route_features import (
    extract_route_features,
    should_call_query_intent_llm,
    structural_route,
)


def test_simple_single_topic_keeps_fast_path_without_variants():
    """验证“simplesingletopickeepsfastpathwithoutvariants”场景的预期行为与回归边界。"""
    plan = build_query_plan("请帮我查一下RAG混合检索")
    assert plan.complexity == "simple"
    assert plan.routing_state == "simple"
    assert plan.retrieval_queries == ()
    assert not plan.use_query_rewrite
    assert not should_call_query_intent_llm("请帮我查一下RAG混合检索")


def test_negated_complex_markers_do_not_upgrade_simple_query():
    """验证“negatedcomplexmarkersdonotupgradesimple查询”场景的预期行为与回归边界。"""
    features, decision = structural_route("只查询RAG混合检索，不要比较其它主题，也不要分析原因")
    assert features.negated_operations
    assert decision.complexity == "simple"


def test_multi_clause_connectors_trigger_decomposition():
    """验证“multiclauseconnectorstriggerdecomposition”场景的预期行为与回归边界。"""
    plan = build_query_plan("RAG混合检索现在什么状态，并且SQL索引是否完成，另外说明Agent简历的偏好")
    assert plan.complexity == "complex"
    assert plan.use_decomposition
    assert len(plan.retrieval_queries) >= 2


def test_question_mark_multi_hop_is_split():
    """验证“question标记multihop是否为切分”场景的预期行为与回归边界。"""
    plan = build_query_plan("查RAG混合检索？再查SQL索引？最后把两条结果合并成一个结论")
    assert plan.complexity == "complex"
    assert plan.use_decomposition
    assert len(plan.retrieval_queries) >= 2


def test_chinese_full_stop_splits_independent_questions(monkeypatch):
    """验证“chinesefullstopsplitsindependentquestions”场景的预期行为与回归边界。"""
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
    """验证“englishcompare是否为complexanddecomposed”场景的预期行为与回归边界。"""
    plan = build_query_plan("Compare Canonical Key and SQL indexes and explain which is more suitable")
    assert plan.complexity == "complex"
    assert plan.use_decomposition
    assert any("Canonical Key" in item for item in plan.retrieval_queries)


def test_anaphora_requires_llm_second_opinion():
    """验证“anaphorarequiresLLMsecondopinion”场景的预期行为与回归边界。"""
    assert should_call_query_intent_llm("那件事呢？")
    features = extract_route_features("那件事呢？")
    assert features.has_anaphora


def test_long_single_scope_query_stays_simple():
    """验证“longsinglescope查询stayssimple”场景的预期行为与回归边界。"""
    features, decision = structural_route(
        "请从长期记忆中只查找RAG这一项主题的唯一当前结论并返回对应的一条记录，不要扩展到其他主题，也不需要额外解释背景和上下文"
    )
    assert len(features.normalized_query) > 50
    assert decision.complexity == "simple"
    assert not should_call_query_intent_llm(features.normalized_query)


def test_causal_query_gets_step_back():
    """验证“causal查询gets步骤back”场景的预期行为与回归边界。"""
    plan = build_query_plan("为什么RAG学习进度发生变化，结合之前记录解释原因")
    assert plan.complexity == "complex"
    assert plan.use_step_back
    assert any(item.endswith("背景与原因") for item in plan.retrieval_queries)


def test_trace_fields_are_bounded_and_explainable():
    """验证“追踪fieldsareboundedandexplainable”场景的预期行为与回归边界。"""
    plan = build_query_plan("比较Canonical Key和SQL索引的当前结论")
    assert 0 <= plan.routing_confidence <= 1
    assert plan.routing_reasons
    assert len(plan.retrieval_queries) <= 4


def test_validated_model_plan_adds_bounded_subquestions():
    """验证“validated模型规划addsboundedsubquestions”场景的预期行为与回归边界。"""
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
    """验证“simple模型规划cannotemitvariants”场景的预期行为与回归边界。"""
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
