"""文件作用：Stage 6 混合检索和性能。

项目关系：本文件依赖 `memory.extractor`、`memory.models`、`memory.repository`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from memory.extractor import extract_candidates, may_contain_memory
from memory.candidate_retriever import candidate_similarity
from memory.canonicalizer import canonicalize_candidate
from memory.models import MemoryCandidate
from memory.models import memory_key_for
from memory.repository import hybrid_adjudication_candidates, insert_memory
from memory.candidate_retriever import retrieve_candidates


def test_memory_key_v2_keeps_preference_polarity_in_one_slot():
    """函数功能：`test_memory_key_v2_keeps_preference_polarity_in_one_slot` 负责验证 memory key v2 keeps preference polarity in one slot 场景，服务于本文件职责：Stage 6 混合检索和性能。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    positive = memory_key_for("preference", subject="用户", predicate="preference", object_value="牛奶", content="用户喜欢喝牛奶")
    negative = memory_key_for("preference", subject="用户", predicate="preference", object_value="牛奶", content="用户讨厌喝牛奶")

    assert positive == negative
    assert positive.endswith(":global")


def test_memory_key_v2_splits_open_semantic_facts_by_topic():
    """函数功能：`test_memory_key_v2_splits_open_semantic_facts_by_topic` 负责验证 memory key v2 splits open semantic facts by topic 场景，服务于本文件职责：Stage 6 混合检索和性能。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    cat = memory_key_for("semantic", subject="用户", predicate="fact", object_value="猫", content="用户养了一只猫")
    guitar = memory_key_for("semantic", subject="用户", predicate="fact", object_value="吉他", content="用户会弹吉他")

    assert cat != guitar
    assert cat.startswith("semantic:")
    assert ":fact:" in cat


def test_rules_extractor_supports_multiple_candidates_for_one_note():
    """函数功能：`test_rules_extractor_supports_multiple_candidates_for_one_note` 负责验证 rules extractor supports multiple candidates for one note 场景，服务于本文件职责：Stage 6 混合检索和性能。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    candidates = extract_candidates("note-multi", "今天参加了日语交流会，发现我更喜欢小班练习，下周要继续报名。")
    types = {candidate.memory_type for candidate in candidates}

    assert {"episodic", "preference", "task"}.issubset(types)


def test_may_contain_memory_is_lightweight_gate():
    """函数功能：`test_may_contain_memory_is_lightweight_gate` 负责验证 may contain memory is lightweight gate 场景，服务于本文件职责：Stage 6 混合检索和性能。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    assert may_contain_memory("你好") is False
    assert may_contain_memory("我讨厌喝牛奶") is True
    assert may_contain_memory("今天参加了日语交流会") is True


def test_hybrid_adjudication_exact_key_survives_large_similar_set():
    """函数功能：`test_hybrid_adjudication_exact_key_survives_large_similar_set` 负责验证 hybrid adjudication exact key survives large similar set 场景，服务于本文件职责：Stage 6 混合检索和性能。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    target = None
    for index in range(130):
        created = insert_memory(
            "space-hybrid",
            extract_candidates(f"note-{index}", f"我喜欢喝饮品A{index}")[0],
            source_note_id=f"note-{index}",
        )
        if index == 0:
            target = created

    candidate = extract_candidates("note-change", "我现在不喜欢喝饮品A0了")[0]
    results = hybrid_adjudication_candidates("space-hybrid", candidate, limit=8)

    assert target is not None
    assert results
    assert results[0].id == target.id


def test_task_family_channel_recalls_related_instance_without_authorizing_merge():
    first = canonicalize_candidate(MemoryCandidate(
        "task", "完成检索质量优化第一轮", 0.8, 0.95,
        task_status="todo", subject="用户", predicate="检索质量优化第一轮",
        scope={"operation": "执行", "scope": "global"},
    ))
    stored = insert_memory("space-task-family-channel", first, source_note_id="note-first")
    second = canonicalize_candidate(MemoryCandidate(
        "task", "完成检索质量优化第二轮", 0.8, 0.95,
        task_status="done", subject="用户", predicate="检索质量优化第二轮",
        scope={"operation": "执行", "scope": "global"},
    ))

    results = hybrid_adjudication_candidates("space-task-family-channel", second, limit=8)

    assert stored.id in {memory.id for memory in results}
    assert candidate_similarity(second, stored) == 0.75


def test_preference_family_channel_recalls_old_related_preference_outside_structured_window():
    target = insert_memory(
        "space-preference-family-channel",
        canonicalize_candidate(MemoryCandidate(
            "preference", "用户喜欢喝拿铁", 0.8, 0.95,
            subject="用户", predicate="preference", object_value="拿铁",
            evidence_span="用户喜欢喝拿铁", scope={"scope": "global"}, polarity="positive",
        )),
        source_note_id="note-latte",
    )
    # These unrelated lexical matches ensure the family anchor must survive
    # multi-channel retrieval before deterministic identity reranking.
    for index in range(35):
        insert_memory(
            "space-preference-family-channel",
            canonicalize_candidate(MemoryCandidate(
                "preference", f"用户喜欢设备P{index}", 0.7, 0.9,
                subject="用户", predicate="preference", object_value=f"设备P{index}",
                evidence_span=f"用户喜欢设备P{index}", scope={"scope": "global"}, polarity="positive",
            )),
            source_note_id=f"note-device-{index}",
        )

    candidate = canonicalize_candidate(MemoryCandidate(
        "preference", "用户喜欢喝咖啡", 0.8, 0.95,
        subject="用户", predicate="preference", object_value="咖啡",
        evidence_span="用户喜欢喝咖啡", scope={"scope": "global"}, polarity="positive",
    ))
    results = retrieve_candidates("space-preference-family-channel", candidate, limit=8)

    assert target.id in {memory.id for memory in results}
    assert candidate_similarity(candidate, target) == 0.84
