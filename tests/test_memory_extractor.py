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
