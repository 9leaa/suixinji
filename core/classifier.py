"""LLM-backed note classifier with structured output."""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.llm_client import complete_json

import json

from core.taxonomy import (
    GLOBAL_TAGS,
    NOTE_TYPES,
    TYPE_TAGS,
    normalize_classification_data,
    normalize_tags,
)


class NoteClassification(BaseModel):
    """表示 LLM 对一条笔记文本的结构化分类结果。

    功能说明:
        约束大模型输出，使分类器稳定返回标题、标签、主类型和摘要。

    传参说明:
        title: 一句话标题，控制在 20 个汉字以内。
        tags: 2 到 5 个中文标签。
        type: 主类型，例如灵感、学习、任务、生活、情绪、资料、日记。
        summary: 一句话摘要，说明这条笔记的核心内容。

    返回类型说明:
        NoteClassification: 一条笔记文本的结构化分类结果实例。
    """

    title: str = Field(description="一句话标题，控制在 20 个汉字以内")
    tags: list[str] = Field(description="2 到 5 个中文标签")
    type: str = Field(description="主类型，例如 任务、学习、灵感、资料、生活、情绪")
    summary: str = Field(description="一句话摘要，说明这条笔记的核心内容")


SYSTEM_PROMPT = f"""
你是“随心记 Agent”的笔记分类器。

你的任务：
1. 给用户随手发来的内容生成一个短标题。
2. 判断一个固定主类型 type。
3. 提取 2 到 5 个中文 tags。
4. 写一句简洁摘要。

type 必须且只能从这里选择一个：
{NOTE_TYPES}

tags 规则：
- 必须从“当前 type 推荐标签”和“全局推荐标签”中选择。
- 如果原文中出现了具体事物，但不在标签池里，不要把它作为 tag，可以体现在 title 或 summary 里。

全局推荐标签：
{GLOBAL_TAGS}

每类推荐标签：
{json.dumps(TYPE_TAGS, ensure_ascii=False, indent=2)}

输出要求：
- 不要编造原文没有的信息。
- 你必须只输出 JSON object，不要输出 markdown，不要解释。
- JSON 字段必须且只能包含：title, tags, type, summary。
- tags 必须是字符串数组。

输出示例：
{{"title":"吃馅饼","tags":["饮食","日常"],"type":"生活","summary":"记录了一次吃馅饼的日常内容。"}}
"""


def classify_text(text: str) -> NoteClassification:
    """调用 LLM 对原始笔记文本进行结构化分类。

    功能说明:
        调用统一 LLM 适配层获取 JSON object，再使用 NoteClassification 进行本地校验，
        将用户原始文本转换为标题、标签、主类型和摘要。

    传参说明:
        text: 用户输入的原始笔记文本。

    返回类型说明:
        NoteClassification: 包含标题、标签、主类型和摘要的分类结果。
    """
    data = complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=text, llm_task="note_classification")
    data = normalize_classification_data(data)
    return NoteClassification.model_validate(data)


def classify_text_local(text: str) -> NoteClassification:
    """Create a deterministic provisional classification without network I/O."""
    value = " ".join(str(text or "").split()).strip()
    lowered = value.casefold()

    if any(marker in lowered for marker in ("记得", "待办", "todo", "需要", "提醒", "计划", "跟进", "截止", "完成", "修复", "实现")):
        note_type = "任务"
        tags = ["待办", "提醒"]
    elif any(marker in value for marker in ("焦虑", "压力", "疲惫", "低落", "开心", "烦躁", "平静", "感激", "情绪")):
        note_type = "情绪"
        emotion_tag = next((tag for tag in TYPE_TAGS["情绪"] if tag in value), "困惑")
        tags = [emotion_tag, "日常"]
    elif any(marker in lowered for marker in ("学习", "课程", "书", "论文", "练习", "知识", "研究", "教程")):
        note_type = "学习"
        tags = ["笔记", "知识点"]
    elif any(marker in value for marker in ("灵感", "创意", "想法", "设计", "可以做", "改进")):
        note_type = "灵感"
        tags = ["创意", "想法"]
    elif "http://" in lowered or "https://" in lowered or any(marker in value for marker in ("链接", "文档", "收藏", "备查", "资料")):
        note_type = "资料"
        tags = ["备查", "收藏"]
    else:
        note_type = "生活"
        if any(marker in value for marker in ("吃", "喝", "咖啡", "茶", "水果", "餐")):
            tags = ["饮食", "日常"]
        elif any(marker in value for marker in ("跑步", "健身", "运动", "游泳", "骑行")):
            tags = ["运动", "日常"]
        else:
            tags = ["日常", "长期"]

    title_source = value.split("。", 1)[0].split("！", 1)[0].split("？", 1)[0].strip(" ，,；;")
    title = title_source[:20] or "随手记录"
    summary = value[:120] or "一条随手记录。"
    return NoteClassification(
        title=title,
        tags=normalize_tags(tags, note_type),
        type=note_type,
        summary=summary,
    )
