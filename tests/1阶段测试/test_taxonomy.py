from core.taxonomy import (
    NOTE_TYPES,
    allowed_tags_for_type,
    is_valid_tag,
    is_valid_type,
    normalize_classification_data,
    normalize_tag,
    normalize_type,
    normalize_tags,
)


def test_fixed_types_and_tags_are_valid():
    assert is_valid_type("任务")
    assert is_valid_type("生活")
    assert not is_valid_type("工作")

    assert is_valid_tag("饮食")
    assert is_valid_tag("#提醒")
    assert not is_valid_tag("随便生成的标签")


def test_normalize_type_falls_back_to_resource_type():
    assert normalize_type("任务") == "任务"
    assert normalize_type("不存在的类型") == "资料"
    assert normalize_type(None) == "资料"


def test_normalize_tags_keeps_only_allowed_fixed_tags():
    tags = normalize_tags(
        ["待办", "提醒", "任务", "自由标签", "#截止时间", "待办"],
        "任务",
    )

    assert tags == ["待办", "提醒", "截止时间"]
    assert "任务" not in tags
    assert "自由标签" not in tags


def test_normalize_tags_fills_missing_tags_from_type_pool():
    tags = normalize_tags([], "生活")

    assert len(tags) == 2
    assert all(tag in allowed_tags_for_type("生活") for tag in tags)


def test_normalize_classification_data_normalizes_type_and_tags():
    data = normalize_classification_data(
        {
            "title": "买菜",
            "type": "生活",
            "tags": ["饮食", "自由标签", "#日常"],
            "summary": "记录买菜。",
        }
    )

    assert data["type"] in NOTE_TYPES
    assert data["tags"] == ["饮食", "日常"]


def test_normalize_tag_strips_hash_and_spaces():
    assert normalize_tag(" #饮食 ") == "饮食"
