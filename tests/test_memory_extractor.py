"""文件作用：rules/LLM/hybrid 抽取与回退。

项目关系：本文件依赖 `memory`、`memory.extractor`；被 暂无静态导入方或仅作为入口脚本执行。
"""


import pytest

from memory import extractor
from memory.extractor import extract_candidates
from memory.models import MemoryCandidate


def _preference_candidate(
    *,
    content: str,
    evidence_span: str,
    topic: str,
    memory_key: str,
    polarity: str = "positive",
    qualifiers: list[str] | None = None,
) -> MemoryCandidate:
    return MemoryCandidate(
        memory_type="preference",
        content=content,
        importance=0.8,
        confidence=0.9,
        note_id="preference-hybrid",
        subject="用户",
        predicate="preference",
        object_value=topic,
        evidence_span=evidence_span,
        memory_key=memory_key,
        polarity=polarity,
        scope={"scope": "global", "qualifiers": qualifiers or []},
    )


def test_extractor_filters_low_value_text():
    """函数功能：`test_extractor_filters_low_value_text` 负责验证 extractor filters low value text 场景，服务于本文件职责：rules/LLM/hybrid 抽取与回退。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    assert extract_candidates("note-1", "你好") == []
    assert extract_candidates("note-2", "哈哈") == []
    assert extract_candidates("note-3", "可能以后会学 Java") == []


def test_extractor_classifies_preference_and_semantic():
    """函数功能：`test_extractor_classifies_preference_and_semantic` 负责验证 extractor classifies preference and semantic 场景，服务于本文件职责：rules/LLM/hybrid 抽取与回退。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    candidates = extract_candidates("note-1", "我现在不想继续学习 Java，短期重点放在 Python Agent。")
    types = {candidate.memory_type for candidate in candidates}

    assert "preference" in types
    assert "semantic" in types
    assert any("Java" in candidate.entities for candidate in candidates)


def test_extractor_classifies_task_status():
    """函数功能：`test_extractor_classifies_task_status` 负责验证 extractor classifies task status 场景，服务于本文件职责：rules/LLM/hybrid 抽取与回退。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    candidates = extract_candidates("note-1", "记得完善随心记项目 README。")

    assert len(candidates) == 1
    assert candidates[0].memory_type == "task"
    assert candidates[0].task_status == "todo"


def test_extractor_treats_allergy_as_preference_constraint():
    """函数功能：`test_extractor_treats_allergy_as_preference_constraint` 负责验证 extractor treats allergy as preference constraint 场景，服务于本文件职责：rules/LLM/hybrid 抽取与回退。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    candidates = extract_candidates("note-1", "我苹果过敏")

    assert candidates
    assert candidates[0].memory_type == "preference"


def test_extractor_treats_dislike_as_preference_constraint():
    """函数功能：`test_extractor_treats_dislike_as_preference_constraint` 负责验证 extractor treats dislike as preference constraint 场景，服务于本文件职责：rules/LLM/hybrid 抽取与回退。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    candidates = extract_candidates("note-1", "我讨厌喝牛奶")

    assert candidates
    assert candidates[0].memory_type == "preference"
    assert "讨厌喝牛奶" in candidates[0].content
    assert "牛奶" in candidates[0].entities


def test_preference_extractor_uses_the_object_as_topic_not_the_sentence_template():
    """函数功能：`test_preference_extractor_uses_the_object_as_topic_not_the_sentence_template` 负责验证 preference extractor uses the object as topic not the sentence template 场景，服务于本文件职责：rules/LLM/hybrid 抽取与回退。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    latte = extract_candidates("note-1", "我喜欢喝燕麦拿铁，通常选大杯")[0]
    apple = extract_candidates("note-2", "我喜欢吃苹果")[0]

    assert latte.subject == "用户"
    assert latte.predicate == "preference"
    assert latte.object_value == "燕麦拿铁"
    assert apple.object_value == "苹果"


@pytest.mark.parametrize(
    ("text", "topic"),
    [
        ("我喜欢用量子键盘", "量子键盘"),
        ("我喜欢听爵士乐", "爵士乐"),
        ("我暂时不用机械键盘", "机械键盘"),
        ("我对花生过敏", "花生"),
    ],
)
def test_preference_topic_extraction_is_grammar_based(text, topic):
    """函数功能：`test_preference_topic_extraction_is_grammar_based` 负责验证 preference topic extraction is grammar based 场景，服务于本文件职责：rules/LLM/hybrid 抽取与回退。
    传参：
        text: 输入文本内容。
        topic: topic 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    candidate = extract_candidates("note-generic", text)[0]

    assert candidate.object_value == topic


def test_llm_extractor_returns_structured_candidates(monkeypatch):
    """函数功能：`test_llm_extractor_returns_structured_candidates` 负责验证 llm extractor returns structured candidates 场景，服务于本文件职责：rules/LLM/hybrid 抽取与回退。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "llm")
    monkeypatch.setattr(
        extractor,
        "complete_json",
        lambda **kwargs: {
            "candidates": [
                {
                    "memory_type": "task",
                    "entity": "用户",
                    "attribute": "Agent 实习",
                    "operation": "准备",
                    "canonical_topic": "准备 Agent 实习",
                    "content": "准备 Agent 实习",
                    "subject": "Agent 实习",
                    "predicate": "task",
                    "object": "准备 Agent 实习",
                    # 兼容尚未更新的模型；抽取层必须在写入前归并为 todo。
                    "task_status": "in_progress",
                    "confidence": 0.91,
                    "importance": 0.88,
                    "evidence_span": "重点准备 Agent 实习",
                    "extraction_reason": "明确的当前任务",
                    "entities": ["Agent"],
                    "should_store": True,
                }
            ]
        },
    )

    candidates = extractor.extract_candidates("note-1", "我最近重点准备 Agent 实习")

    assert len(candidates) == 1
    assert candidates[0].memory_type == "task"
    assert candidates[0].task_status == "todo"
    assert candidates[0].predicate in {"task", "Agent实习"}
    assert candidates[0].candidate_id == extractor.candidate_id_for("note-1", "task", "准备 Agent 实习")


def test_llm_preference_qualifiers_are_grounded_and_separate_from_topic(monkeypatch):
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "llm")
    monkeypatch.setattr(
        extractor,
        "complete_json",
        lambda **kwargs: {
            "candidates": [
                {
                    "memory_type": "preference",
                    "entity": "用户",
                    "attribute": "preference",
                    "operation": None,
                    "canonical_topic": "无糖浓咖啡",
                    "task_status": None,
                    "polarity": "positive",
                    "scope": None,
                    "qualifiers": ["sugar_free", "strong", "invented"],
                    "old_value": None,
                    "new_value": "无糖浓咖啡",
                    "content": "用户喜欢无糖浓咖啡",
                    "evidence_span": "喜欢无糖浓咖啡",
                    "confidence": 0.91,
                    "importance": 0.8,
                    "should_store": True,
                    "extraction_reason": "明确偏好",
                    "entities": ["咖啡"],
                }
            ]
        },
    )

    candidates = extractor.extract_candidates("llm-pref-qualifiers", "我喜欢无糖浓咖啡")

    assert len(candidates) == 1
    assert candidates[0].object_value == "咖啡"
    assert candidates[0].polarity == "positive"
    assert candidates[0].scope["qualifiers"] == ["sugar_free", "strong"]


def test_rule_extractor_keeps_mixed_atoms_and_multiple_preferences(monkeypatch):
    monkeypatch.setattr(extractor.settings, "MEMORY_CLAUSE_EXTRACTION_ENABLED", True)
    mixed = extractor.extract_rule_candidates(
        "mixed-atoms",
        "我喜欢咖啡，主要使用MacBook Pro，这周要完成随心记评测。",
    )
    assert {candidate.memory_type for candidate in mixed} >= {"preference", "semantic", "task"}

    preferences = extractor.extract_rule_candidates("multi-pref", "我喜欢咖啡和绿茶。")
    preference_rows = [candidate for candidate in preferences if candidate.memory_type == "preference"]
    assert len(preference_rows) == 2
    assert {candidate.evidence_span for candidate in preference_rows} == {"喜欢咖啡", "绿茶"}
    assert {candidate.polarity for candidate in preference_rows} == {"positive"}


def test_hybrid_only_repairs_uncovered_atom_types(monkeypatch):
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "hybrid")
    monkeypatch.setattr(extractor.settings, "MEMORY_CLAUSE_EXTRACTION_ENABLED", True)
    monkeypatch.setattr(
        extractor,
        "complete_json",
        lambda **kwargs: {
            "candidates": [{
                "memory_type": "preference", "entity": "用户", "attribute": "preference", "operation": None,
                "canonical_topic": "咖啡", "task_status": None, "old_value": None, "new_value": "咖啡",
                "content": "用户喜欢咖啡", "evidence_span": "我喜欢咖啡", "confidence": 0.9,
                "importance": 0.8, "should_store": True, "extraction_reason": "明确偏好", "entities": ["咖啡"],
            }]
        },
    )
    rows = extractor.extract_candidates(
        "hybrid-atoms",
        "我喜欢咖啡，主要使用MacBook Pro，这周要完成随心记评测。",
    )
    assert [row.memory_type for row in rows].count("preference") == 1
    assert {row.memory_type for row in rows} >= {"preference", "semantic", "task"}
    repaired = [row for row in rows if row.extraction_reason == "hybrid_atom_coverage_repair"]
    assert {row.memory_type for row in repaired} >= {"semantic", "task"}


def test_hybrid_repairs_a_second_preference_in_the_same_clause(monkeypatch):
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "hybrid")
    monkeypatch.setattr(
        extractor,
        "complete_json",
        lambda **kwargs: {
            "candidates": [{
                "memory_type": "preference", "entity": "用户", "attribute": "preference", "operation": None,
                "canonical_topic": "咖啡", "task_status": None, "polarity": "positive", "old_value": None,
                "new_value": "咖啡", "content": "用户喜欢咖啡", "evidence_span": "喜欢咖啡",
                "confidence": 0.9, "importance": 0.8, "should_store": True, "extraction_reason": "明确偏好", "entities": ["咖啡"],
            }]
        },
    )

    rows = extractor.extract_candidates("hybrid-two-preferences", "我喜欢咖啡和绿茶。")

    preferences = [row for row in rows if row.memory_type == "preference"]
    assert {row.object_value for row in preferences} == {"咖啡", "绿茶"}
    assert len(preferences) == 2
    assert any(row.extraction_reason == "hybrid_atom_coverage_repair" and row.object_value == "绿茶" for row in preferences)


def test_hybrid_prefers_known_llm_polarity_and_backfills_unknown(monkeypatch):
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "hybrid")

    def extract_with(polarity: str):
        monkeypatch.setattr(
            extractor,
            "complete_json",
            lambda **kwargs: {
                "candidates": [{
                    "memory_type": "preference", "entity": "用户", "attribute": "preference", "operation": None,
                    "canonical_topic": "咖啡", "task_status": None, "polarity": polarity, "old_value": None,
                    "new_value": "咖啡", "content": "用户喜欢咖啡", "evidence_span": "喜欢咖啡",
                    "confidence": 0.9, "importance": 0.8, "should_store": True, "extraction_reason": "明确偏好", "entities": ["咖啡"],
                }]
            },
        )
        return [row for row in extractor.extract_candidates(f"hybrid-polarity-{polarity}", "我喜欢咖啡。") if row.memory_type == "preference"]

    assert extract_with("negative")[0].polarity == "negative"
    assert extract_with("unknown")[0].polarity == "positive"


def test_hybrid_merges_one_preference_when_only_the_qualifier_key_drifts():
    model = _preference_candidate(
        content="用户喜欢无糖咖啡",
        evidence_span="喜欢无糖咖啡",
        topic="咖啡",
        memory_key="preference:用户:咖啡:global",
    )
    rule = _preference_candidate(
        content="用户喜欢无糖咖啡",
        evidence_span="喜欢无糖咖啡",
        topic="咖啡",
        memory_key="preference:用户:咖啡:global:sugarfree",
        qualifiers=["sugar_free"],
    )

    rows = extractor._merge_uncovered_rule_candidates([model], [rule])

    assert len(rows) == 1
    assert rows[0].scope["qualifiers"] == ["sugar_free"]


def test_hybrid_preference_merge_keeps_distinct_same_sentence_topics():
    model = _preference_candidate(
        content="用户喜欢咖啡",
        evidence_span="喜欢咖啡和绿茶",
        topic="咖啡",
        memory_key="preference:用户:咖啡:global",
    )
    coffee_rule = _preference_candidate(
        content="用户喜欢咖啡",
        evidence_span="喜欢咖啡",
        topic="咖啡",
        memory_key="preference:用户:咖啡:global:sugarfree",
        qualifiers=["sugar_free"],
    )
    tea_rule = _preference_candidate(
        content="用户喜欢绿茶",
        evidence_span="绿茶",
        topic="绿茶",
        memory_key="preference:用户:绿茶:global",
    )

    rows = extractor._merge_uncovered_rule_candidates([model], [coffee_rule, tea_rule])

    assert len(rows) == 2
    assert {row.object_value for row in rows} == {"咖啡", "绿茶"}
    assert any(row.extraction_reason == "hybrid_atom_coverage_repair" and row.object_value == "绿茶" for row in rows)


def test_hybrid_preference_merge_never_combines_opposite_polarities():
    model = _preference_candidate(
        content="用户不喜欢咖啡",
        evidence_span="不喜欢咖啡",
        topic="咖啡",
        memory_key="preference:用户:咖啡:global",
        polarity="negative",
    )
    rule = _preference_candidate(
        content="用户喜欢咖啡",
        evidence_span="不喜欢咖啡",
        topic="咖啡",
        memory_key="preference:用户:咖啡:global:sugarfree",
        polarity="positive",
        qualifiers=["sugar_free"],
    )

    rows = extractor._merge_uncovered_rule_candidates([model], [rule])

    assert len(rows) == 2


def test_hybrid_does_not_duplicate_single_clause_llm_candidate(monkeypatch):
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "hybrid")
    monkeypatch.setattr(
        extractor,
        "complete_json",
        lambda **kwargs: {"candidates": [{
            "memory_type": "task", "entity": "用户", "attribute": "数据库",
            "operation": "迁移", "canonical_topic": "迁移数据库",
            "task_status": "done", "content": "数据库迁移已经完成",
            "evidence_span": "数据库迁移已经完成", "confidence": 0.9,
            "importance": 0.8, "should_store": True, "extraction_reason": "task",
            "entities": ["数据库迁移"],
        }]},
    )
    rows = extractor.extract_candidates("single-clause", "数据库迁移已经完成。")
    assert len(rows) == 1
    assert rows[0].extractor_type == "llm"


def test_hybrid_task_coverage_uses_stable_goal_not_exact_surface_key():
    model = extractor.normalize_candidate_v3(
        MemoryCandidate(
            memory_type="task", content="继续处理检索质量优化", importance=0.9, confidence=0.9,
            evidence_span="继续处理检索质量优化", subject="用户", predicate="检索质量优化",
            object_value="检索质量优化", task_status="todo", extractor_type="llm",
            scope={"operation": "处理", "scope": "global"},
        )
    )
    rule = extractor.normalize_candidate_v3(
        MemoryCandidate(
            memory_type="task", content="继续处理检索质量优化第一轮", importance=0.8, confidence=0.8,
            evidence_span="继续处理检索质量优化第一轮", subject="用户", predicate="检索质量优化第一轮",
            object_value="检索质量优化第一轮", task_status="todo", extractor_type="rules",
            scope={"operation": "维护", "scope": "global"},
        )
    )

    rows = extractor._merge_uncovered_rule_candidates([model], [rule])

    assert len(rows) == 1
    assert rows[0].extractor_type == "llm"


def test_hybrid_task_coverage_merges_a_self_contained_project_reference():
    model = extractor.normalize_candidate_v3(
        MemoryCandidate(
            memory_type="task", content="这个项目还要继续做", importance=0.9, confidence=0.9,
            evidence_span="这个项目还要继续做", subject="用户", predicate="这个项目项目",
            object_value="项目", task_status="todo", extractor_type="llm", scope={"operation": "制作", "scope": "global"},
        )
    )
    rule = extractor.normalize_candidate_v3(
        MemoryCandidate(
            memory_type="task", content="这个项目还要继续做", importance=0.8, confidence=0.8,
            evidence_span="这个项目还要继续做", subject="用户", predicate="这个项目还要继续",
            object_value="这个项目还要继续做", task_status="todo", extractor_type="rules", scope={"operation": "制作", "scope": "global"},
        )
    )

    assert len(extractor._merge_uncovered_rule_candidates([model], [rule])) == 1


def test_hybrid_task_coverage_merges_a_shared_blocker_with_title_noise():
    model = extractor.normalize_candidate_v3(
        MemoryCandidate(
            memory_type="task", content="飞书接入改造其实还在等测试数据", importance=0.9, confidence=0.9,
            evidence_span="飞书接入改造其实还在等测试数据", subject="用户", predicate="飞书接入改造状态",
            object_value="状态", task_status="todo", extractor_type="llm",
            scope={"operation": "等待", "scope": "global", "blocker": "等待测试数据"},
        )
    )
    rule = extractor.normalize_candidate_v3(
        MemoryCandidate(
            memory_type="task", content="飞书接入改造其实还在等测试数据", importance=0.8, confidence=0.8,
            evidence_span="飞书接入改造其实还在等测试数据", subject="用户", predicate="飞书接入改造其实还在等测试数据",
            object_value="飞书接入改造其实还在等测试数据", task_status="todo", extractor_type="rules",
            scope={"operation": "维护", "scope": "global", "blocker": "等待测试数据"},
        )
    )

    assert len(extractor._merge_uncovered_rule_candidates([model], [rule])) == 1


def test_hybrid_semantic_coverage_keeps_llm_stable_slot_over_generic_rule_fact():
    model = extractor.normalize_candidate_v3(
        MemoryCandidate(
            memory_type="semantic", content="用户主要使用MacBook Pro", importance=0.9, confidence=0.9,
            evidence_span="主要使用MacBook Pro", subject="用户", predicate="primary_device",
            object_value="MacBook Pro", extractor_type="llm", scope={"scope": "current"},
        )
    )
    rule = extractor.normalize_candidate_v3(
        MemoryCandidate(
            memory_type="semantic", content="用户主要使用MacBook Pro", importance=0.8, confidence=0.8,
            evidence_span="主要使用MacBook Pro", subject="用户", predicate="fact",
            object_value="主要使用MacBook Pro", extractor_type="rules", scope={"scope": "current"},
        )
    )

    rows = extractor._merge_uncovered_rule_candidates([model], [rule])

    assert len(rows) == 1
    assert rows[0].extractor_type == "llm"
    assert rows[0].predicate == "primary_device"


def test_hybrid_semantic_coverage_keeps_ambiguous_generic_rule_fact():
    evidence = "我家在北京且主要使用MacBook Pro"
    location = extractor.normalize_candidate_v3(
        MemoryCandidate(
            memory_type="semantic", content="用户家在北京", importance=0.9, confidence=0.9,
            evidence_span=evidence, subject="用户", predicate="location", object_value="北京",
            extractor_type="llm", scope={"scope": "current"},
        )
    )
    device = extractor.normalize_candidate_v3(
        MemoryCandidate(
            memory_type="semantic", content="用户主要使用MacBook Pro", importance=0.9, confidence=0.9,
            evidence_span=evidence, subject="用户", predicate="primary_device", object_value="MacBook Pro",
            extractor_type="llm", scope={"scope": "current"},
        )
    )
    rule = extractor.normalize_candidate_v3(
        MemoryCandidate(
            memory_type="semantic", content=evidence, importance=0.8, confidence=0.8,
            evidence_span=evidence, subject="用户", predicate="fact", object_value=evidence,
            extractor_type="rules", scope={"scope": "current"},
        )
    )

    rows = extractor._merge_uncovered_rule_candidates([location, device], [rule])

    assert len(rows) == 3
    assert {row.predicate for row in rows} == {"location", "primary_device", "fact"}


def test_hybrid_semantic_coverage_keeps_rule_when_llm_has_no_semantic_candidate():
    rule = extractor.normalize_candidate_v3(
        MemoryCandidate(
            memory_type="semantic", content="用户主要使用MacBook Pro", importance=0.8, confidence=0.8,
            evidence_span="主要使用MacBook Pro", subject="用户", predicate="fact",
            object_value="主要使用MacBook Pro", extractor_type="rules", scope={"scope": "current"},
        )
    )

    rows = extractor._merge_uncovered_rule_candidates([], [rule])

    assert len(rows) == 1
    assert rows[0].predicate == "fact"
    assert rows[0].extractor_type == "rules"


def test_hybrid_episodic_coverage_accepts_a_concise_event_title():
    model = extractor.normalize_candidate_v3(
        MemoryCandidate(
            memory_type="episodic", content="昨天在学校参加了项目评审", importance=0.9, confidence=0.9,
            evidence_span="昨天在学校参加了项目评审", subject="用户", predicate="event",
            object_value="参加项目评审", extractor_type="llm", scope={"scope": "history"},
        )
    )
    rule = extractor.normalize_candidate_v3(
        MemoryCandidate(
            memory_type="episodic", content="昨天在学校参加了项目评审", importance=0.8, confidence=0.8,
            evidence_span="昨天在学校参加了项目评审", subject="用户", predicate="event",
            object_value="昨天在学校参加了项目评审", extractor_type="rules", scope={"scope": "history"},
        )
    )

    rows = extractor._merge_uncovered_rule_candidates([model], [rule])

    assert len(rows) == 1
    assert rows[0].extractor_type == "llm"


def test_rule_evidence_excludes_terminal_sentence_punctuation():
    rows = extractor.extract_rule_candidates("punctuation", "数据库迁移已经完成。")
    assert rows
    assert all(row.evidence_span == "数据库迁移已经完成" for row in rows)


def test_rule_reference_fallback_inherits_recent_task_identity():
    previous = [{
        "note_id": "n-prev",
        "sequence_no": 9,
        "role": "user",
        "text": "随心记第三阶段评测还在继续处理。",
        "sensitive": False,
    }]
    rows = extractor.extract_candidates(
        "reference-fallback",
        "这个也做完了。",
        previous_messages=previous,
    )
    assert len(rows) == 1
    assert rows[0].memory_type == "task"
    assert rows[0].task_status == "done"
    assert rows[0].scope["reference_status"] == "resolved"
    assert rows[0].scope["antecedent_note_id"] == "n-prev"
    assert rows[0].scope["antecedent_offset"] == -1
    assert "第三阶段评测" in str(rows[0].scope["canonical_topic"])


def test_rule_reference_fallback_rejects_multiple_identifiable_task_offsets():
    previous = [
        {"note_id": "n-old-task", "sequence_no": 7, "role": "user", "text": "数据库迁移还在继续处理。", "sensitive": False},
        {"note_id": "n-chat", "sequence_no": 8, "role": "user", "text": "今天心情不错。", "sensitive": False},
        {"note_id": "n-near-task", "sequence_no": 9, "role": "user", "text": "随心记第三阶段评测还在继续处理。", "sensitive": False},
    ]
    rows = extractor.extract_candidates("reference-near", "这个也做完了。", previous_messages=previous)

    assert rows == []


def test_rule_reference_fallback_rejects_ambiguous_recent_tasks():
    previous = [
        {"note_id": "n-old-task", "sequence_no": 8, "role": "user", "text": "数据库迁移还在继续处理。", "sensitive": False},
        {"note_id": "n-near-task", "sequence_no": 9, "role": "user", "text": "随心记第三阶段评测还没做完。", "sensitive": False},
    ]

    rows = extractor.extract_candidates("reference-ambiguous", "这个继续做吧。", previous_messages=previous)

    assert rows == []


def test_rule_reference_fallback_derives_second_user_message_offset():
    previous = [
        {"note_id": "n-task", "sequence_no": 8, "role": "user", "text": "数据库迁移还在继续处理。", "sensitive": False},
        {"note_id": "n-chat", "sequence_no": 9, "role": "user", "text": "今天心情不错。", "sensitive": False},
    ]
    rows = extractor.extract_candidates("reference-second", "这个也做完了。", previous_messages=previous)

    assert len(rows) == 1
    assert rows[0].scope["antecedent_note_id"] == "n-task"
    assert rows[0].scope["antecedent_offset"] == -2


def test_previous_messages_are_only_sent_for_reference_signals(monkeypatch):
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "llm")
    payloads = []

    def fake_complete_json(**kwargs):
        import json

        payloads.append(json.loads(kwargs["user_prompt"]))
        return {"candidates": []}

    monkeypatch.setattr(extractor, "complete_json", fake_complete_json)
    previous = [
        {"note_id": f"n{index}", "offset": -index, "text": f"历史消息{index}"}
        for index in range(1, 5)
    ]
    extractor.extract_candidates("plain", "记得完成测试报告", previous_messages=previous)
    extractor.extract_candidates("reference", "这个也做完了", previous_messages=previous)

    assert payloads[0]["previous_messages"] == []
    assert [row["offset"] for row in payloads[1]["previous_messages"]] == [-1, -2, -3]


def test_resolved_reference_metadata_is_preserved_in_candidate_scope(monkeypatch):
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "llm")
    monkeypatch.setattr(
        extractor,
        "complete_json",
        lambda **kwargs: {"candidates": [{
            "memory_type": "task", "entity": "随心记", "attribute": "测试报告", "operation": "完成",
            "canonical_topic": "完成随心记测试报告", "task_status": "done", "old_value": None,
            "new_value": None, "content": "测试报告已结束", "evidence_span": "这个也做完了",
            "confidence": 0.96, "importance": 0.8, "should_store": True, "extraction_reason": "近三轮唯一指代",
            "entities": ["随心记"], "reference_status": "resolved", "antecedent_note_id": "n1",
            "antecedent_offset": -1, "antecedent_evidence_span": "记得完成随心记测试报告", "resolution_confidence": 0.96,
        }]},
    )
    rows = extractor.extract_candidates(
        "reference",
        "这个也做完了",
        previous_messages=[{"note_id": "n1", "offset": -1, "text": "记得完成随心记测试报告"}],
    )
    assert len(rows) == 1
    assert rows[0].scope["reference_status"] == "resolved"
    assert rows[0].scope["antecedent_note_id"] == "n1"
    assert rows[0].evidence_span == "这个也做完了"


def test_llm_reference_validation_derives_contextual_offset(monkeypatch):
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "llm")
    monkeypatch.setattr(
        extractor,
        "complete_json",
        lambda **kwargs: {"candidates": [{
            "memory_type": "task", "entity": "随心记", "attribute": "测试报告", "operation": "完成",
            "canonical_topic": "完成随心记测试报告", "task_status": "done", "old_value": None,
            "new_value": None, "content": "这个也做完了", "evidence_span": "这个也做完了",
            "confidence": 0.96, "importance": 0.8, "should_store": True, "extraction_reason": "近三轮唯一指代",
            "entities": ["随心记"], "reference_status": "resolved", "antecedent_note_id": "n1",
            "antecedent_offset": None, "antecedent_evidence_span": "记得完成随心记测试报告", "resolution_confidence": 0.96,
        }]},
    )
    rows = extractor.extract_candidates(
        "reference-validation",
        "这个也做完了",
        previous_messages=[{"note_id": "n1", "sequence_no": 9, "role": "user", "text": "记得完成随心记测试报告", "sensitive": False}],
    )

    assert len(rows) == 1
    assert rows[0].scope["reference_status"] == "resolved"
    assert rows[0].scope["antecedent_offset"] == -1


def test_hybrid_reference_resolution_does_not_duplicate_rule_coverage(monkeypatch):
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "hybrid")
    monkeypatch.setattr(
        extractor,
        "complete_json",
        lambda **kwargs: {"candidates": [{
            "memory_type": "task", "entity": "随心记", "attribute": "测试报告", "operation": "完成",
            "canonical_topic": "完成随心记测试报告", "task_status": "done", "old_value": None,
            "new_value": None, "content": "这个也做完了", "evidence_span": "这个也做完了",
            "confidence": 0.96, "importance": 0.8, "should_store": True, "extraction_reason": "近三轮任务指代",
            "entities": ["随心记"], "reference_status": "resolved", "antecedent_note_id": "n1",
            "antecedent_offset": None, "antecedent_evidence_span": "记得完成随心记测试报告", "resolution_confidence": 0.96,
        }]},
    )
    rows = extractor.extract_candidates(
        "reference-hybrid-dedupe",
        "这个也做完了",
        previous_messages=[{"note_id": "n1", "sequence_no": 9, "role": "user", "text": "记得完成随心记测试报告", "sensitive": False}],
    )

    assert len(rows) == 1
    assert rows[0].scope["reference_status"] == "resolved"


def test_llm_extractor_falls_back_to_rules(monkeypatch):
    """函数功能：`test_llm_extractor_falls_back_to_rules` 负责验证 llm extractor falls back to rules 场景，服务于本文件职责：rules/LLM/hybrid 抽取与回退。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "llm")
    monkeypatch.setattr(extractor, "complete_json", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("model down")))

    candidates = extractor.extract_candidates("note-1", "我讨厌喝牛奶")

    assert candidates
    assert candidates[0].memory_type == "preference"
    assert candidates[0].reason == "llm_failed_rule_fallback"


def test_strict_llm_evaluation_does_not_score_rules_fallback(monkeypatch):
    """Strict evaluation propagates LLM failures; production fallback stays default."""
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "hybrid")
    monkeypatch.setattr(extractor, "complete_json", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("model down")))

    with pytest.raises(RuntimeError, match="model down"):
        extractor.extract_candidates("note-strict", "我讨厌喝牛奶", allow_llm_failure_fallback=False)


def test_llm_fallback_logs_safe_degradation_without_raw_text_or_keys(monkeypatch):
    """函数功能：`test_llm_fallback_logs_safe_degradation_without_raw_text_or_keys` 负责验证 llm fallback logs safe degradation without raw text or keys 场景，服务于本文件职责：rules/LLM/hybrid 抽取与回退。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "hybrid")
    events = []
    raw_text = "我不喜欢喝牛奶，我也不爱工作，现在正在投递agent简历"
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    error = (
        "LLM chat completion failed; model='fast-model', "
        f"base_url='https://example.test/v1?api_key={secret}', "
        f"text_preview='{raw_text}', output_preview='{raw_text}', cause=APITimeoutError."
    )
    monkeypatch.setattr(extractor, "complete_json", lambda **kwargs: (_ for _ in ()).throw(RuntimeError(error)))
    monkeypatch.setattr(extractor, "log_event", lambda action, **kwargs: events.append((action, kwargs)))

    candidates = extractor.extract_candidates("note-safe-log", raw_text)

    assert candidates
    assert candidates[0].reason == "llm_failed_rule_fallback"
    assert events
    action, payload = events[0]
    assert action == "memory.extractor.llm_failed"
    assert payload["status"] == "degraded"
    assert payload["record_id"] == "note-safe-log"
    assert payload["extra"]["extractor_mode"] == "hybrid"
    assert payload["extra"]["fallback"] == "rules"
    assert payload["extra"]["fallback_reason"] == "llm_failed_rule_fallback"
    serialized = str(payload)
    assert raw_text not in serialized
    assert secret not in serialized
    assert "text_preview=[redacted]" in serialized
    assert "output_preview=[redacted]" in serialized


def test_memory_v3_e2e_diagnostic_prefix_is_ignored_for_rule_fallback(monkeypatch):
    """函数功能：`test_memory_v3_e2e_diagnostic_prefix_is_ignored_for_rule_fallback` 负责验证 memory v3 e2e diagnostic prefix is ignored for rule fallback 场景，服务于本文件职责：rules/LLM/hybrid 抽取与回退。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "llm")
    monkeypatch.setattr(extractor, "complete_json", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("model down")))

    candidates = extractor.extract_candidates("note-marker", "[MemoryV3-E2E-20260727102715] 我讨厌喝牛奶")

    assert candidates
    assert candidates[0].memory_type == "preference"
    assert "[MemoryV3-E2E" not in candidates[0].content
    assert candidates[0].evidence_span == "我讨厌喝牛奶"


def test_extractor_filters_secret_shaped_values_before_model_call(monkeypatch):
    """函数功能：`test_extractor_filters_secret_shaped_values_before_model_call` 负责验证 extractor filters secret shaped values before model call 场景，服务于本文件职责：rules/LLM/hybrid 抽取与回退。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "llm")
    monkeypatch.setattr(extractor, "complete_json", lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not call model")))

    assert extractor.extract_candidates("note-1", "API Key: sk-abcdefghijklmnop") == []
