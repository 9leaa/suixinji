from __future__ import annotations

from agent.query_planner import build_query_plan


def test_simple_query_keeps_fast_path():
    """验证“simple查询keepsfastpath”场景的预期行为与回归边界。"""
    plan = build_query_plan("我现在住在哪里")
    assert plan.complexity == "simple"
    assert plan.retrieval_queries == ()
    assert not plan.use_query_rewrite
    assert not plan.use_decomposition
    assert not plan.use_step_back


def test_complex_query_enables_bounded_step_back():
    """验证“complex查询enablesbounded步骤back”场景的预期行为与回归边界。"""
    plan = build_query_plan("为什么我最近的RAG学习进度变化了，结合之前的记录总结原因")
    assert plan.complexity == "complex"
    assert plan.use_step_back
    assert len(plan.retrieval_queries) <= 4
    assert any(query.endswith("背景与原因") for query in plan.retrieval_queries)



def test_english_step_back_is_complex():
    """验证“english步骤back是否为complex”场景的预期行为与回归边界。"""
    plan = build_query_plan("对这个主题做step-back分析后给出结论")
    assert plan.complexity == "complex"
    assert plan.use_step_back
    assert any(query.endswith("上位概念、背景与约束") for query in plan.retrieval_queries)


def test_comparison_is_decomposed_into_topics():
    """验证“comparison是否为decomposedintotopics”场景的预期行为与回归边界。"""
    plan = build_query_plan("比较Canonical Key和SQL索引的当前结论")
    assert plan.use_decomposition
    assert "Canonical Key" in plan.retrieval_queries
    assert "SQL索引" in plan.retrieval_queries


def test_preference_question_has_neutral_query_polarity():
    """验证“偏好question是否包含neutral查询polarity”场景的预期行为与回归边界。"""
    from memory.policies.preference import preference_query_polarity

    assert preference_query_polarity("工作日早上我喜欢喝咖啡吗？") == "unknown"
    assert preference_query_polarity("我是不是不喜欢苹果") == "unknown"
    assert preference_query_polarity("我不喜欢工作日早上喝咖啡") == "negative"
    assert preference_query_polarity("我喜欢喝什么") == "positive"
    assert preference_query_polarity("我不喜欢喝什么") == "negative"


def _record(content: str, *, memory_type: str = "semantic", object_value: str | None = None, polarity: str | None = None):
    """验证“记录”场景的预期行为与回归边界。"""
    from memory.models import MemoryRecord, normalize_content

    return MemoryRecord(
        id=normalize_content(content)[:20],
        space_id="test",
        memory_type=memory_type,
        content=content,
        normalized_content=normalize_content(content),
        importance=0.8,
        confidence=0.9,
        status="active",
        valid_from=None,
        valid_until=None,
        created_at="2026-07-25T00:00:00+08:00",
        updated_at="2026-07-25T00:00:00+08:00",
        last_accessed_at=None,
        access_count=0,
        current_version=1,
        object_value=object_value,
        polarity=polarity,
    )


def test_topic_score_beats_same_type_prior():
    """验证“topic评分beatssame类型prior”场景的预期行为与回归边界。"""
    from memory.retriever import score_memory

    query = "SQL索引这项事实目前怎么描述"
    relevant = _record("用户当前关注SQL索引设计", object_value="SQL索引")
    unrelated = _record("用户当前关注Python异步学习", object_value="Python异步")
    assert score_memory(query, relevant) > score_memory(query, unrelated) + 0.4


def test_preference_question_does_not_suppress_negative_memory():
    """验证“偏好questiondoesnotsuppressnegative记忆”场景的预期行为与回归边界。"""
    from memory.retriever import score_memory

    query = "工作日早上我喜欢喝咖啡吗？"
    negative = _record(
        "用户不喜欢工作日早上喝咖啡",
        memory_type="preference",
        object_value="工作日早上咖啡",
        polarity="negative",
    )
    unrelated = _record(
        "用户喜欢下午喝乌龙茶",
        memory_type="preference",
        object_value="乌龙茶",
        polarity="positive",
    )
    assert score_memory(query, negative) > score_memory(query, unrelated) + 0.4


def test_cjk_identifiers_require_complete_family_match():
    """验证“cjkidentifiersrequire完成family匹配”场景的预期行为与回归边界。"""
    from memory.retriever import _overlap_score

    query = "学习-05 测试策略任务状态"
    expected = "用户正在处理测试策略；记忆编号学习-05"
    suffix_collision = "用户当前关注储蓄计划；记忆编号财务学习-05"
    assert _overlap_score(query, expected) > _overlap_score(query, suffix_collision) + 0.3


def test_explicit_task_intent_penalizes_same_label_semantic_memory():
    """验证“explicit任务意图penalizessamelabelsemantic记忆”场景的预期行为与回归边界。"""
    from memory.retriever import score_memory

    query = "当前Agent简历是什么状态"
    task = _record("用户正在处理Agent简历", memory_type="task", object_value="Agent简历")
    semantic = _record("用户当前关注记忆Agent", memory_type="semantic", object_value="记忆Agent")
    assert score_memory(query, task) > score_memory(query, semantic) + 0.3


def test_informational_preference_wording_is_neutral():
    """验证“informational偏好wording是否为neutral”场景的预期行为与回归边界。"""
    from memory.policies.preference import preference_query_polarity

    assert preference_query_polarity("生活-02 工作日咖啡对应的偏好") == "unknown"


def test_state_layer_wording_is_not_misrouted_as_task_status():
    """验证“状态layerwording是否为notmisroutedas任务状态”场景的预期行为与回归边界。"""
    from memory.retriever import score_memory

    query = "请从状态记忆里找Agent的记忆Agent"
    semantic = _record("用户当前关注记忆Agent", memory_type="semantic", object_value="记忆Agent")
    task = _record("用户需要完成Agent评测", memory_type="task", object_value="Agent评测")
    assert score_memory(query, semantic) > score_memory(query, task) + 0.2
