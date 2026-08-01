"""文件作用：Note 分类。

项目关系：本文件依赖 `core.llm_client`、`core.taxonomy`；被 `core.worker`、`eval.eval_classification`。
"""



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
    """类功能：`NoteClassification` 封装与“Note 分类”相关的数据结构、状态或行为。
    继承关系：继承 `BaseModel`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
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
    """函数功能：`classify_text` 负责分类 text，服务于本文件职责：Note 分类。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `NoteClassification` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    data = complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=text, llm_task="note_classification")
    data = normalize_classification_data(data)
    return NoteClassification.model_validate(data)


def classify_text_local(text: str) -> NoteClassification:
    """函数功能：`classify_text_local` 负责分类 text local，服务于本文件职责：Note 分类。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `NoteClassification` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
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
