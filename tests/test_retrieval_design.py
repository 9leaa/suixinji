"""文件作用：Note 与 Memory 混合检索、引用和覆盖。

项目关系：本文件依赖 `agent.query_planner`、`memory.models`、`memory.policies.preference`、`memory.retriever`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from __future__ import annotations

from agent.query_planner import build_query_plan


def test_simple_query_keeps_fast_path():
    """函数功能：`test_simple_query_keeps_fast_path` 负责验证 simple query keeps fast path 场景，服务于本文件职责：Note 与 Memory 混合检索、引用和覆盖。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    plan = build_query_plan("我现在住在哪里")
    assert plan.complexity == "simple"
    assert plan.retrieval_queries == ()
    assert not plan.use_query_rewrite
    assert not plan.use_decomposition
    assert not plan.use_step_back


def test_complex_query_enables_bounded_step_back():
    """函数功能：`test_complex_query_enables_bounded_step_back` 负责验证 complex query enables bounded step back 场景，服务于本文件职责：Note 与 Memory 混合检索、引用和覆盖。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    plan = build_query_plan("为什么我最近的RAG学习进度变化了，结合之前的记录总结原因")
    assert plan.complexity == "complex"
    assert plan.use_step_back
    assert len(plan.retrieval_queries) <= 4
    assert any(query.endswith("背景与原因") for query in plan.retrieval_queries)



def test_english_step_back_is_complex():
    """函数功能：`test_english_step_back_is_complex` 负责验证 english step back is complex 场景，服务于本文件职责：Note 与 Memory 混合检索、引用和覆盖。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    plan = build_query_plan("对这个主题做step-back分析后给出结论")
    assert plan.complexity == "complex"
    assert plan.use_step_back
    assert any(query.endswith("上位概念、背景与约束") for query in plan.retrieval_queries)


def test_comparison_is_decomposed_into_topics():
    """函数功能：`test_comparison_is_decomposed_into_topics` 负责验证 comparison is decomposed into topics 场景，服务于本文件职责：Note 与 Memory 混合检索、引用和覆盖。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    plan = build_query_plan("比较Canonical Key和SQL索引的当前结论")
    assert plan.use_decomposition
    assert "Canonical Key" in plan.retrieval_queries
    assert "SQL索引" in plan.retrieval_queries


def test_preference_question_has_neutral_query_polarity():
    """函数功能：`test_preference_question_has_neutral_query_polarity` 负责验证 preference question has neutral query polarity 场景，服务于本文件职责：Note 与 Memory 混合检索、引用和覆盖。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    from memory.policies.preference import preference_query_polarity

    assert preference_query_polarity("工作日早上我喜欢喝咖啡吗？") == "unknown"
    assert preference_query_polarity("我是不是不喜欢苹果") == "unknown"
    assert preference_query_polarity("我不喜欢工作日早上喝咖啡") == "negative"
    assert preference_query_polarity("我喜欢喝什么") == "positive"
    assert preference_query_polarity("我不喜欢喝什么") == "negative"


def _record(content: str, *, memory_type: str = "semantic", object_value: str | None = None, polarity: str | None = None):
    """函数功能：`_record` 负责记录，服务于本文件职责：Note 与 Memory 混合检索、引用和覆盖。
    传参：
        content: 需要处理、保存或展示的文本内容，类型为 `str`。
        memory_type: memory type 参数，由调用方传入，类型为 `str`，默认值为 `'semantic'`。
        object_value: object value 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        polarity: polarity 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
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
    """函数功能：`test_topic_score_beats_same_type_prior` 负责验证 topic score beats same type prior 场景，服务于本文件职责：Note 与 Memory 混合检索、引用和覆盖。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    from memory.retriever import score_memory

    query = "SQL索引这项事实目前怎么描述"
    relevant = _record("用户当前关注SQL索引设计", object_value="SQL索引")
    unrelated = _record("用户当前关注Python异步学习", object_value="Python异步")
    assert score_memory(query, relevant) > score_memory(query, unrelated) + 0.4


def test_preference_question_does_not_suppress_negative_memory():
    """函数功能：`test_preference_question_does_not_suppress_negative_memory` 负责验证 preference question does not suppress negative memory 场景，服务于本文件职责：Note 与 Memory 混合检索、引用和覆盖。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`test_cjk_identifiers_require_complete_family_match` 负责验证 cjk identifiers require complete family match 场景，服务于本文件职责：Note 与 Memory 混合检索、引用和覆盖。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    from memory.retriever import _overlap_score

    query = "学习-05 测试策略任务状态"
    expected = "用户正在处理测试策略；记忆编号学习-05"
    suffix_collision = "用户当前关注储蓄计划；记忆编号财务学习-05"
    assert _overlap_score(query, expected) > _overlap_score(query, suffix_collision) + 0.3


def test_explicit_task_intent_penalizes_same_label_semantic_memory():
    """函数功能：`test_explicit_task_intent_penalizes_same_label_semantic_memory` 负责验证 explicit task intent penalizes same label semantic memory 场景，服务于本文件职责：Note 与 Memory 混合检索、引用和覆盖。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    from memory.retriever import score_memory

    query = "当前Agent简历是什么状态"
    task = _record("用户正在处理Agent简历", memory_type="task", object_value="Agent简历")
    semantic = _record("用户当前关注记忆Agent", memory_type="semantic", object_value="记忆Agent")
    assert score_memory(query, task) > score_memory(query, semantic) + 0.3


def test_informational_preference_wording_is_neutral():
    """函数功能：`test_informational_preference_wording_is_neutral` 负责验证 informational preference wording is neutral 场景，服务于本文件职责：Note 与 Memory 混合检索、引用和覆盖。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    from memory.policies.preference import preference_query_polarity

    assert preference_query_polarity("生活-02 工作日咖啡对应的偏好") == "unknown"


def test_state_layer_wording_is_not_misrouted_as_task_status():
    """函数功能：`test_state_layer_wording_is_not_misrouted_as_task_status` 负责验证 state layer wording is not misrouted as task status 场景，服务于本文件职责：Note 与 Memory 混合检索、引用和覆盖。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    from memory.retriever import score_memory

    query = "请从状态记忆里找Agent的记忆Agent"
    semantic = _record("用户当前关注记忆Agent", memory_type="semantic", object_value="记忆Agent")
    task = _record("用户需要完成Agent评测", memory_type="task", object_value="Agent评测")
    assert score_memory(query, semantic) > score_memory(query, task) + 0.2
