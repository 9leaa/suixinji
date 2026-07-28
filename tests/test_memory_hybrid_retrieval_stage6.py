from memory.extractor import extract_candidates, may_contain_memory
from memory.models import memory_key_for
from memory.repository import hybrid_adjudication_candidates, insert_memory


def test_memory_key_v2_keeps_preference_polarity_in_one_slot():
    """验证“记忆键v2keeps偏好polarityoneslot”场景的预期行为与回归边界。"""
    positive = memory_key_for("preference", subject="用户", predicate="preference", object_value="牛奶", content="用户喜欢喝牛奶")
    negative = memory_key_for("preference", subject="用户", predicate="preference", object_value="牛奶", content="用户讨厌喝牛奶")

    assert positive == negative
    assert positive.endswith(":global")


def test_memory_key_v2_splits_open_semantic_facts_by_topic():
    """验证“记忆键v2splits打开semanticfactsbytopic”场景的预期行为与回归边界。"""
    cat = memory_key_for("semantic", subject="用户", predicate="fact", object_value="猫", content="用户养了一只猫")
    guitar = memory_key_for("semantic", subject="用户", predicate="fact", object_value="吉他", content="用户会弹吉他")

    assert cat != guitar
    assert cat.startswith("semantic:")
    assert ":fact:" in cat


def test_rules_extractor_supports_multiple_candidates_for_one_note():
    """验证“rulesextractorsupportsmultiplecandidatesforone笔记”场景的预期行为与回归边界。"""
    candidates = extract_candidates("note-multi", "今天参加了日语交流会，发现我更喜欢小班练习，下周要继续报名。")
    types = {candidate.memory_type for candidate in candidates}

    assert {"episodic", "preference", "task"}.issubset(types)


def test_may_contain_memory_is_lightweight_gate():
    """验证“maycontain记忆是否为lightweightgate”场景的预期行为与回归边界。"""
    assert may_contain_memory("你好") is False
    assert may_contain_memory("我讨厌喝牛奶") is True
    assert may_contain_memory("今天参加了日语交流会") is True


def test_hybrid_adjudication_exact_key_survives_large_similar_set():
    """验证“混合adjudicationexact键surviveslargesimilar设置”场景的预期行为与回归边界。"""
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
