"""文件作用：查询意图识别。

项目关系：本文件依赖 `core.llm_client`、`memory.prompts`；被 `agent.query_agent`。
"""



from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from core.llm_client import complete_json
from memory.prompts import QUERY_INTENT_PROMPT


class QueryIntent(BaseModel):
    """类功能：`QueryIntent` 封装与“查询意图识别”相关的数据结构、状态或行为。
    继承关系：继承 `BaseModel`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    intent: Literal[
        "task_status",
        "preference",
        "current_fact",
        "note_history",
        "recent_notes",
        "relationship",
        "summary",
        "general_search",
    ]
    entity: str | None = None
    attribute: str | None = None
    topic: str | None = None
    time_scope: Literal["current", "history", "recent", "all"] | None = None
    confidence: float = Field(ge=0, le=1)
    complexity: Literal["simple", "complex", "uncertain"] = "uncertain"
    strategies: list[Literal["none", "rewrite", "decomposition", "step_back"]] = Field(default_factory=list)
    rewritten_queries: list[str] = Field(default_factory=list)
    sub_questions: list[dict[str, Any]] = Field(default_factory=list)
    step_back_query: str | None = None

    @field_validator("entity", "attribute", "topic", mode="before")
    @classmethod
    def _clean_optional_text(cls, value: object) -> str | None:
        """函数功能：`QueryIntent._clean_optional_text` 在类 `QueryIntent` 中负责清理 optional text，服务于本文件职责：查询意图识别。
        传参：
            value: 待转换、校验或计算的值，类型为 `object`。
        返回结果说明：
            返回 `str | None`；未命中或无需处理时可返回 `None`。
        """
        if value is None:
            return None
        text = str(value).strip()
        return text[:160] or None


def classify_query_intent(question: str) -> QueryIntent | None:
    """函数功能：`classify_query_intent` 负责分类 query intent，服务于本文件职责：查询意图识别。
    传参：
        question: 用户问题文本，类型为 `str`。
    返回结果说明：
        返回 `QueryIntent | None`；未命中或无需处理时可返回 `None`。
    """
    question = str(question or "").strip()
    if not question:
        return None
    try:
        data = complete_json(
            system_prompt=QUERY_INTENT_PROMPT,
            user_prompt=json.dumps({"question": question}, ensure_ascii=False),
            model_role="fast",
            llm_task="query_intent",
        )
        return QueryIntent.model_validate(data)
    except (ValidationError, TypeError, ValueError):
        return None
    except Exception:
        # 模型或网络失败时刻意回退到既有的确定性和 ReAct 路由。
        return None


def _retrieval_query(question: str, intent: QueryIntent) -> str:
    """函数功能：`_retrieval_query` 负责查询 retrieval，服务于本文件职责：查询意图识别。
    传参：
        question: 用户问题文本，类型为 `str`。
        intent: intent 参数，由调用方传入，类型为 `QueryIntent`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    terms = [str(question or "").strip()]
    terms.extend(str(value).strip() for value in (intent.topic, intent.attribute, intent.entity) if value)
    terms.extend(re.findall(r"[A-Za-z][A-Za-z0-9+#._-]*", question))
    return " ".join(dict.fromkeys(term for term in terms if term))[:600]


def is_task_inventory_question(question: str) -> bool:
    """函数功能：`is_task_inventory_question` 负责判断是否为 task inventory question，服务于本文件职责：查询意图识别。
    传参：
        question: 用户问题文本，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    compact = re.sub(r"\s+", "", str(question or "")).casefold()
    has_task_noun = any(marker in compact for marker in ("任务", "待办", "todo"))
    has_listing_cue = any(marker in compact for marker in ("哪些", "有什么", "有啥", "清单", "列表", "列出"))
    return has_task_noun and has_listing_cue


def _query_plan_payload(intent: QueryIntent) -> dict[str, object]:
    """函数功能：`_query_plan_payload` 负责查询 plan payload，服务于本文件职责：查询意图识别。
    传参：
        intent: intent 参数，由调用方传入，类型为 `QueryIntent`。
    返回结果说明：
        返回 `dict[str, object]`，表示结构化结果、载荷或状态映射。
    """
    return {
        "complexity": intent.complexity,
        "strategies": list(intent.strategies),
        "rewritten_queries": list(intent.rewritten_queries),
        "sub_questions": list(intent.sub_questions),
        "step_back_query": intent.step_back_query,
    }


def route_for_intent(intent: QueryIntent, question: str, *, memory_min_score: float, note_min_score: float, top_k: int) -> dict[str, object] | None:
    """函数功能：`route_for_intent` 负责路由 for intent，服务于本文件职责：查询意图识别。
    传参：
        intent: intent 参数，由调用方传入，类型为 `QueryIntent`。
        question: 用户问题文本，类型为 `str`。
        memory_min_score: memory min score 参数，由调用方传入，类型为 `float`。
        note_min_score: note min score 参数，由调用方传入，类型为 `float`。
        top_k: top k 参数，由调用方传入，类型为 `int`。
    返回结果说明：
        返回 `dict[str, object] | None`，表示结构化结果、载荷或状态映射。
    """
    memory_type = {
        "task_status": "task",
        "preference": "preference",
        "current_fact": "semantic",
    }.get(intent.intent)
    if memory_type is None:
        # 合法的非 Memory intent 仍需抑制旧版捷径，避免每个短问题都直接进入 Note 语义检索。
        # query agent 将此标记解释为 Memory 预取加 ReAct 流程。
        return {
            "action": "memory_prefetch",
            "args": {},
            "synthesize": True,
            "reason": f"query_intent:{intent.intent}",
            "intent": intent.intent,
            "query_plan": _query_plan_payload(intent),
        }
    retrieval_query = _retrieval_query(question, intent)
    fallback: dict[str, object] = {
        "action": "memory_note_fallback",
        "args": {"query": retrieval_query, "limit": top_k, "min_score": note_min_score},
    }
    if memory_type == "task" and not any((intent.topic, intent.attribute)) and is_task_inventory_question(question):
        fallback = {
            "action": "filter_notes",
            "args": {"type": "任务", "limit": 30},
        }
    action = "task_status_search" if memory_type == "task" else "memory_search"
    return {
        "action": action,
        "args": {
            "query": retrieval_query,
            "memory_type": memory_type,
            "limit": 8 if memory_type == "task" else 5,
            "min_score": memory_min_score,
        },
        "fallback": fallback,
        "synthesize": True,
        "reason": f"query_intent:{intent.intent}",
        "intent": intent.intent,
        "query_plan": _query_plan_payload(intent),
    }
