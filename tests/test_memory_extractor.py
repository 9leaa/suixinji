import pytest

from memory import extractor
from memory.extractor import extract_candidates


def test_extractor_filters_low_value_text():
    assert extract_candidates("note-1", "你好") == []
    assert extract_candidates("note-2", "哈哈") == []
    assert extract_candidates("note-3", "可能以后会学 Java") == []


def test_extractor_classifies_preference_and_semantic():
    candidates = extract_candidates("note-1", "我现在不想继续学习 Java，短期重点放在 Python Agent。")
    types = {candidate.memory_type for candidate in candidates}

    assert "preference" in types
    assert "semantic" in types
    assert any("Java" in candidate.entities for candidate in candidates)


def test_extractor_classifies_task_status():
    candidates = extract_candidates("note-1", "记得完善随心记项目 README。")

    assert len(candidates) == 1
    assert candidates[0].memory_type == "task"
    assert candidates[0].task_status == "todo"


def test_extractor_treats_allergy_as_preference_constraint():
    candidates = extract_candidates("note-1", "我苹果过敏")

    assert candidates
    assert candidates[0].memory_type == "preference"


def test_extractor_treats_dislike_as_preference_constraint():
    candidates = extract_candidates("note-1", "我讨厌喝牛奶")

    assert candidates
    assert candidates[0].memory_type == "preference"
    assert "讨厌喝牛奶" in candidates[0].content
    assert "牛奶" in candidates[0].entities


def test_preference_extractor_uses_the_object_as_topic_not_the_sentence_template():
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
    candidate = extract_candidates("note-generic", text)[0]

    assert candidate.object_value == topic


def test_llm_extractor_returns_structured_candidates(monkeypatch):
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "llm")
    monkeypatch.setattr(
        extractor,
        "complete_json",
        lambda **kwargs: {
            "candidates": [
                {
                    "memory_type": "task",
                    "content": "准备 Agent 实习",
                    "subject": "Agent 实习",
                    "predicate": "task",
                    "object": "准备 Agent 实习",
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
    assert candidates[0].task_status == "in_progress"
    assert candidates[0].predicate == "task"
    assert candidates[0].candidate_id == extractor.candidate_id_for("note-1", "task", "准备 Agent 实习")


def test_llm_extractor_falls_back_to_rules(monkeypatch):
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "llm")
    monkeypatch.setattr(extractor, "complete_json", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("model down")))

    candidates = extractor.extract_candidates("note-1", "我讨厌喝牛奶")

    assert candidates
    assert candidates[0].memory_type == "preference"


def test_extractor_filters_secret_shaped_values_before_model_call(monkeypatch):
    monkeypatch.setattr(extractor, "MEMORY_EXTRACTOR_MODE", "llm")
    monkeypatch.setattr(extractor, "complete_json", lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not call model")))

    assert extractor.extract_candidates("note-1", "API Key: sk-abcdefghijklmnop") == []
