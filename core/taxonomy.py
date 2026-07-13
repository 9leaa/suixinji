"""Shared note taxonomy for fixed note types and tags."""

from __future__ import annotations

from typing import Any


NOTE_TYPES = ["任务", "学习", "灵感", "资料", "生活", "情绪"]

GLOBAL_TAGS = [
    "重要",
    "待处理",
    "计划",
    "复盘",
    "问题",
    "决定",
    "提醒",
    "想法",
    "总结",
    "长期",
    "短期",
    "待确认",
]

TYPE_TAGS = {
    "任务": [
        "待办",
        "进行中",
        "已完成",
        "优先级",
        "截止时间",
        "阻塞",
        "跟进",
        "安排",
        "协作",
        "检查",
    ],
    "学习": [
        "概念",
        "方法",
        "笔记",
        "课程",
        "书籍",
        "论文",
        "练习",
        "疑问",
        "总结",
        "知识点",
    ],
    "灵感": [
        "创意",
        "观察",
        "设计",
        "写作",
        "产品想法",
        "改进",
        "灵光一闪",
        "素材",
        "可能性",
    ],
    "资料": [
        "链接",
        "文档",
        "收藏",
        "引用",
        "清单",
        "工具",
        "教程",
        "数据",
        "案例",
        "备查",
    ],
    "生活": [
        "饮食",
        "运动",
        "睡眠",
        "健康",
        "出行",
        "消费",
        "家务",
        "社交",
        "娱乐",
        "日常",
    ],
    "情绪": [
        "开心",
        "焦虑",
        "压力",
        "疲惫",
        "低落",
        "期待",
        "烦躁",
        "平静",
        "感激",
        "困惑",
    ],
}

ALL_TAGS = set(GLOBAL_TAGS)
for tags in TYPE_TAGS.values():
    ALL_TAGS.update(tags)


def is_valid_type(value: Any) -> bool:
    """Return whether value is one of the fixed note types."""
    return str(value or "").strip() in NOTE_TYPES


def is_valid_tag(value: Any) -> bool:
    """Return whether value is one of the fixed allowed tags."""
    return str(value or "").strip().lstrip("#") in ALL_TAGS


def normalize_type(value: Any) -> str:
    """Normalize a model-produced type to one fixed note type."""
    note_type = str(value or "").strip()
    if note_type in NOTE_TYPES:
        return note_type

    return "资料"


def normalize_tag(value: Any) -> str:
    """Normalize a tag string without accepting aliases or free tags."""
    return str(value or "").strip().lstrip("#")


def allowed_tags_for_type(note_type: str) -> list[str]:
    """Return tags available for a given note type."""
    return TYPE_TAGS.get(note_type, []) + GLOBAL_TAGS


def normalize_tags(raw_tags: Any, note_type: str) -> list[str]:
    """Keep only fixed-pool tags and fill missing tags from the type pool.

    Rules:
    - tags must come from TYPE_TAGS[note_type] or GLOBAL_TAGS.
    - tags cannot duplicate.
    - tags cannot equal note_type.
    - output length is 2 to 5 when possible.
    """
    if not isinstance(raw_tags, list):
        raw_tags = []

    allowed = set(allowed_tags_for_type(note_type))
    result: list[str] = []
    seen: set[str] = set()

    for item in raw_tags:
        tag = normalize_tag(item)
        if tag not in allowed:
            continue
        if tag == note_type:
            continue
        if tag in seen:
            continue

        seen.add(tag)
        result.append(tag)

        if len(result) >= 5:
            return result

    for tag in allowed_tags_for_type(note_type):
        if len(result) >= 2:
            break
        if tag in seen:
            continue
        if tag == note_type:
            continue

        seen.add(tag)
        result.append(tag)

    return result[:5]


def normalize_classification_data(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize raw LLM classification output before Pydantic validation."""
    note_type = normalize_type(data.get("type"))

    return {
        **data,
        "type": note_type,
        "tags": normalize_tags(data.get("tags"), note_type),
    }