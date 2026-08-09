"""Layer-1 field contract and deterministic normalization regression tests."""

from memory.canonicalizer import normalize_candidate_v3, task_family_compatible, task_instance_authorized
from memory.extraction_schema import parse_extracted_candidate
from memory import extractor
from memory.extractor import extract_candidates
from memory.models import MemoryCandidate


def _candidate(memory_type: str, text: str, **kwargs) -> MemoryCandidate:
    return MemoryCandidate(
        memory_type,
        text,
        0.8,
        0.9,
        evidence_span=text,
        subject=kwargs.get("subject"),
        predicate=kwargs.get("predicate"),
        object_value=kwargs.get("object_value"),
        task_status=kwargs.get("task_status"),
        scope=kwargs.get("scope", {}),
    )


def test_preference_contract_normalizes_topic_and_polarity():
    positive = extract_candidates("p1", "我喜欢喝燕麦拿铁")[0]
    negative = extract_candidates("p2", "我不喜欢喝燕麦拿铁")[0]
    assert positive.predicate == negative.predicate == "preference"
    assert positive.object_value == negative.object_value == "燕麦拿铁"
    assert positive.polarity == "positive"
    assert negative.polarity == "negative"
    assert positive.effective_memory_key == negative.effective_memory_key


def test_task_identity_key_is_stable_across_status():
    todo = normalize_candidate_v3(_candidate("task", "记得完善 README", predicate="task", task_status="todo"))
    done = normalize_candidate_v3(_candidate("task", "已经完善 README", predicate="task", task_status="done"))
    assert todo.task_status == "todo"
    assert done.task_status == "done"
    assert todo.effective_memory_key == done.effective_memory_key
    assert todo.scope["canonical_topic"] == done.scope["canonical_topic"]


def test_task_identity_ignores_progress_and_blocker_detail_but_respects_scope():
    base = normalize_candidate_v3(
        _candidate("task", "继续处理检索质量优化", predicate="检索质量优化", task_status="todo", scope={"operation": "处理"})
    )
    blocked = normalize_candidate_v3(
        _candidate(
            "task",
            "继续处理检索质量优化第一轮，现在被接口文档不完整卡住",
            predicate="检索质量优化第一轮被接口文档不完整卡住",
            task_status="todo",
            scope={"operation": "维护"},
        )
    )
    other_scope = normalize_candidate_v3(
        _candidate("task", "继续处理检索质量优化", predicate="检索质量优化", task_status="todo", scope={"operation": "处理", "scope": "工作"})
    )

    assert base.scope["task_family_key"] == blocked.scope["task_family_key"] == "task-family:检索质量优化"
    assert task_family_compatible(base, blocked)
    assert not task_instance_authorized(base, blocked)
    assert not task_family_compatible(base, other_scope)


def test_task_identity_does_not_merge_distinct_explicit_goals():
    report = normalize_candidate_v3(_candidate("task", "完成测试报告", predicate="测试报告", task_status="todo", scope={"operation": "执行"}))
    environment = normalize_candidate_v3(_candidate("task", "完成测试环境", predicate="测试环境", task_status="todo", scope={"operation": "执行"}))

    assert not task_family_compatible(report, environment)


def test_preference_topic_strips_leading_coordination_without_changing_evidence():
    candidate = normalize_candidate_v3(
        _candidate("preference", "和咖啡", predicate="preference", object_value="和咖啡")
    )

    assert candidate.evidence_span == "和咖啡"
    assert candidate.object_value == "咖啡"
    assert candidate.scope["canonical_topic"] == "咖啡"
    assert candidate.scope["preference_family_key"] == "preference-family:用户:饮品"


def test_preference_family_is_business_class_not_topic_or_carrier_word():
    drink = normalize_candidate_v3(_candidate("preference", "我喜欢咖啡", predicate="preference", object_value="咖啡"))
    taste = normalize_candidate_v3(_candidate("preference", "我不喜欢太甜的饮料", predicate="preference", object_value="甜味饮料"))

    assert drink.scope["preference_family_key"] == "preference-family:用户:饮品"
    assert taste.scope["preference_family_key"] == "preference-family:用户:口味"


def test_semantic_attribute_is_enum_and_key_is_stable_for_value_updates():
    beijing = normalize_candidate_v3(_candidate("semantic", "我现在住在北京", predicate="fact", object_value="北京"))
    shanghai = normalize_candidate_v3(_candidate("semantic", "我现在住在上海", predicate="fact", object_value="上海"))
    assert beijing.predicate == shanghai.predicate == "location"
    assert beijing.scope["canonical_topic"] == "用户当前居住地"
    assert beijing.effective_memory_key == shanghai.effective_memory_key


def test_episodic_contract_uses_event_slot():
    event = normalize_candidate_v3(_candidate("episodic", "今天参加了复盘会", predicate="fact"))
    assert event.predicate == "event"
    assert event.object_value
    assert event.scope["canonical_topic"] == event.object_value


def test_schema_rejects_ungrounded_evidence_and_normalizes_cross_type_fields():
    row = {
        "memory_type": "preference",
        "attribute": "drink",
        "task_status": "in_progress",
        "new_value": "饮品偏好",
        "evidence_span": "我喜欢喝咖啡",
        "confidence": 0.9,
        "importance": 0.8,
        "should_store": True,
    }
    parsed = parse_extracted_candidate(row, "我喜欢喝咖啡")
    assert parsed is not None
    assert parsed.attribute == "preference"
    assert parsed.task_status is None
    assert parsed.new_value == "咖啡"
    assert parsed.polarity == "unknown"
    row["evidence_span"] = "原文不存在"
    assert parse_extracted_candidate(row, "我喜欢喝咖啡") is None


def test_schema_accepts_llm_preference_polarity_in_chinese_or_contract_form():
    row = {
        "memory_type": "preference",
        "new_value": "咖啡",
        "polarity": "负向",
        "evidence_span": "我不喜欢咖啡",
        "confidence": 0.9,
        "importance": 0.8,
    }
    parsed = parse_extracted_candidate(row, "我不喜欢咖啡")
    assert parsed is not None
    assert parsed.polarity == "negative"


def test_schema_separates_llm_preference_qualifiers_from_the_topic():
    row = {
        "memory_type": "preference",
        "canonical_topic": "无糖浓咖啡",
        "new_value": "无糖浓咖啡",
        "qualifiers": ["无糖", "strength=strong", "invented"],
        "polarity": "positive",
        "evidence_span": "我喜欢无糖浓咖啡",
        "confidence": 0.9,
        "importance": 0.8,
    }
    parsed = parse_extracted_candidate(row, "我喜欢无糖浓咖啡")
    assert parsed is not None
    assert parsed.canonical_topic == parsed.new_value == "咖啡"
    assert parsed.qualifiers == ["sugar_free", "strong"]


def test_canonicalizer_keeps_llm_qualifiers_in_key_and_records_topic_provenance():
    candidate = _candidate(
        "preference",
        "我喜欢无糖浓咖啡",
        predicate="preference",
        object_value="无糖浓咖啡",
        scope={"canonical_topic": "无糖浓咖啡", "qualifiers": ["sugar_free", "strong"]},
    )
    normalized = normalize_candidate_v3(candidate)
    assert normalized.object_value == "咖啡"
    assert normalized.scope["qualifiers"] == ["sugar_free", "strong"]
    assert normalized.scope["topic_source"] in {"llm_validated", "rules"}
    assert normalized.effective_memory_key == "preference:用户:咖啡:global:sugarfree:strong"


def test_normalization_is_idempotent():
    original = _candidate("preference", "我暂时不用机械键盘", predicate="drink", object_value="泛化偏好")
    once = normalize_candidate_v3(original)
    twice = normalize_candidate_v3(once)
    assert once == twice


def test_llm_structured_output_cannot_override_preference_contract(monkeypatch):
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "llm")
    monkeypatch.setattr(
        extractor,
        "complete_json",
        lambda **_: {"candidates": [{
            "memory_type": "preference", "entity": "用户", "attribute": "drink",
            "operation": "喜欢", "task_status": "todo", "canonical_topic": "饮品偏好",
            "new_value": "饮品偏好", "polarity": "positive", "content": "我喜欢喝咖啡", "evidence_span": "我喜欢喝咖啡",
            "confidence": 0.9, "importance": 0.8, "should_store": True,
        }]},
    )
    candidates = extractor.extract_llm_candidates("llm-contract", "我喜欢喝咖啡")
    assert len(candidates) == 1
    assert candidates[0].predicate == "preference"
    assert candidates[0].object_value == "咖啡"
    assert candidates[0].polarity == "positive"
