"""文件作用：分类词表。

项目关系：本文件依赖 `core.taxonomy`；被 暂无静态导入方或仅作为入口脚本执行。
"""


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
    """函数功能：`test_fixed_types_and_tags_are_valid` 负责验证 fixed types and tags are valid 场景，服务于本文件职责：分类词表。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    assert is_valid_type("任务")
    assert is_valid_type("生活")
    assert not is_valid_type("工作")

    assert is_valid_tag("饮食")
    assert is_valid_tag("#提醒")
    assert not is_valid_tag("随便生成的标签")


def test_normalize_type_falls_back_to_resource_type():
    """函数功能：`test_normalize_type_falls_back_to_resource_type` 负责验证 normalize type falls back to resource type 场景，服务于本文件职责：分类词表。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    assert normalize_type("任务") == "任务"
    assert normalize_type("不存在的类型") == "资料"
    assert normalize_type(None) == "资料"


def test_normalize_tags_keeps_only_allowed_fixed_tags():
    """函数功能：`test_normalize_tags_keeps_only_allowed_fixed_tags` 负责验证 normalize tags keeps only allowed fixed tags 场景，服务于本文件职责：分类词表。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    tags = normalize_tags(
        ["待办", "提醒", "任务", "自由标签", "#截止时间", "待办"],
        "任务",
    )

    assert tags == ["待办", "提醒", "截止时间"]
    assert "任务" not in tags
    assert "自由标签" not in tags


def test_normalize_tags_fills_missing_tags_from_type_pool():
    """函数功能：`test_normalize_tags_fills_missing_tags_from_type_pool` 负责验证 normalize tags fills missing tags from type pool 场景，服务于本文件职责：分类词表。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    tags = normalize_tags([], "生活")

    assert len(tags) == 2
    assert all(tag in allowed_tags_for_type("生活") for tag in tags)


def test_normalize_classification_data_normalizes_type_and_tags():
    """函数功能：`test_normalize_classification_data_normalizes_type_and_tags` 负责验证 normalize classification data normalizes type and tags 场景，服务于本文件职责：分类词表。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`test_normalize_tag_strips_hash_and_spaces` 负责验证 normalize tag strips hash and spaces 场景，服务于本文件职责：分类词表。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    assert normalize_tag(" #饮食 ") == "饮食"
