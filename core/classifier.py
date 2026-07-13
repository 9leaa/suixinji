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
)


# TODO type 这个字段名和 Python 内置函数同名，后续可以考虑改成 category。
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
    data = complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=text)
    data = normalize_classification_data(data)
    return NoteClassification.model_validate(data)
