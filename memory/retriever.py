"""文件作用：长期记忆检索。

项目关系：本文件依赖 `core.settings`、`memory.models`、`memory.policies.preference`、`memory.repository`；被 `memory.consolidator`、`memory.repository`、`repositories.postgres.memory`、`tests.test_retrieval_design`。
"""



from __future__ import annotations

from datetime import datetime
import re

from core.settings import MEMORY_QUERY_MIN_SCORE
from memory.models import MemoryRecord, normalize_content
from memory.policies.preference import preference_polarity, preference_query_polarity


def _parse_ts(value: str | None) -> datetime | None:
    """函数功能：`_parse_ts` 负责解析 ts，服务于本文件职责：长期记忆检索。
    传参：
        value: 待转换、校验或计算的值，类型为 `str | None`。
    返回结果说明：
        返回 `datetime | None`；未命中或无需处理时可返回 `None`。
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


_QUERY_FILLERS = (
    "请优先回答", "请帮我查找", "请帮我查", "请查找", "请回答", "请问", "帮我", "告诉我",
    "结合长期记忆", "根据长期记忆", "长期记忆", "当前信息", "当前状态", "目前状态",
    "长期结论", "相关结论", "相关记忆", "最近记忆", "有什么记忆", "是什么状态",
    "是否已经完成", "是否完成", "需要处理吗", "需要处理", "偏好或习惯是什么", "偏好是什么",
    "习惯是什么", "分别", "比较", "对比", "总结", "归纳", "当前", "目前", "最近", "关于",
    "对应的", "对应", "里的", "是什么", "有哪些", "为什么", "怎么", "如何", "我的", "我对",
    "what is the current task status for", "what is my current memory about", "what is my preference about",
    "current task status", "current memory", "my preference", "what is", "about", "for",
)


def retrieval_topic_text(value: str) -> str:
    """函数功能：`retrieval_topic_text` 负责处理 retrieval topic text，服务于本文件职责：长期记忆检索。
    传参：
        value: 待转换、校验或计算的值，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    text = str(value or "").casefold()
    for filler in sorted(_QUERY_FILLERS, key=len, reverse=True):
        text = text.replace(filler, " ")
    text = re.sub(r"(?:我|用户)(?:现在|当前|最近)?", " ", text)
    text = re.sub(r"(?:不喜欢|喜欢|讨厌|偏好|习惯|要做|待办|任务|进度|完成|取消)", " ", text)
    text = re.sub(r"[的了呢吗么嘛呀啊吧和与及、，,。；;：:！？?!()（）\[\]{}]", " ", text)
    return " ".join(text.split())


def _named_tokens(value: str) -> set[str]:
    # CJK 家族标识（如 生活-02）和 ASCII issue key（如 PROJ-123）同样有意义。
    # 必须匹配完整标识，不能让共享数字后缀把两条无关记录看起来等价。
    """函数功能：`_named_tokens` 负责处理 named tokens，服务于本文件职责：长期记忆检索。
    传参：
        value: 待转换、校验或计算的值，类型为 `str`。
    返回结果说明：
        返回 `set[str]` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    pattern = r"[\u4e00-\u9fffA-Za-z]+-\d+|[A-Za-z][A-Za-z0-9+#._-]*|\d+(?:[._-]\d+)*"
    return {token.casefold() for token in re.findall(pattern, value)}


def _cjk_ngrams(value: str) -> set[str]:
    """函数功能：`_cjk_ngrams` 负责处理 cjk ngrams，服务于本文件职责：长期记忆检索。
    传参：
        value: 待转换、校验或计算的值，类型为 `str`。
    返回结果说明：
        返回 `set[str]` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    grams: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", value):
        if len(run) == 1:
            continue
        for width in (4, 3, 2):
            if len(run) < width:
                continue
            grams.update(run[index : index + width] for index in range(len(run) - width + 1))
    return grams


def _overlap_score(query: str, content: str) -> float:
    """函数功能：`_overlap_score` 负责评分 overlap，服务于本文件职责：长期记忆检索。
    传参：
        query: 检索或查询文本，类型为 `str`。
        content: 需要处理、保存或展示的文本内容，类型为 `str`。
    返回结果说明：
        返回 `float`，表示计算得到的数值结果。
    """
    q_topic = retrieval_topic_text(query)
    c_topic = retrieval_topic_text(content)
    q = normalize_content(q_topic)
    c = normalize_content(c_topic)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    shorter, longer = sorted((q, c), key=len)
    if len(shorter) >= 2 and shorter in longer:
        return min(1.0, 0.82 + 0.18 * len(shorter) / max(len(longer), 1))

    q_named = _named_tokens(q_topic)
    c_named = _named_tokens(c_topic)
    named_score = len(q_named & c_named) / len(q_named) if q_named else 0.0
    q_grams = _cjk_ngrams(q_topic)
    c_grams = _cjk_ngrams(c_topic)
    cjk_score = len(q_grams & c_grams) / len(q_grams) if q_grams else 0.0
    if q_named and q_grams:
        # 共享 Agent 或 SQL 这类泛化 token 时，若中文主题限定词不同，仍不足以判定同一主题。
        return round(0.35 * named_score + 0.65 * cjk_score, 4)
    return round(named_score if q_named else cjk_score, 4)


def _query_memory_type(query: str) -> str | None:
    """函数功能：`_query_memory_type` 负责查询 memory type，服务于本文件职责：长期记忆检索。
    传参：
        query: 检索或查询文本，类型为 `str`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
    value = str(query or "")
    preference = any(marker in value for marker in ("喜欢", "不喜欢", "偏好", "讨厌", "避开", "过敏"))
    task_status_question = any(
        marker in value
        for marker in ("任务状态", "是什么状态", "状态如何", "状态怎么样", "当前状态", "目前状态", "task status")
    ) and not any(marker in value for marker in ("状态记忆", "状态层", "状态或结论"))
    task = any(marker in value for marker in ("待办", "任务", "要做", "需要处理", "进度", "完成", "取消", "task status")) or task_status_question
    semantic = any(marker in value for marker in ("结论", "事实", "关注", "背景", "住哪", "住在"))
    matched = [name for name, enabled in (("preference", preference), ("task", task), ("semantic", semantic)) if enabled]
    return matched[0] if len(matched) == 1 else None


def _intent_score(query: str, memory: MemoryRecord) -> float:
    """函数功能：`_intent_score` 负责评分 intent，服务于本文件职责：长期记忆检索。
    传参：
        query: 检索或查询文本，类型为 `str`。
        memory: memory 参数，由调用方传入，类型为 `MemoryRecord`。
    返回结果说明：
        返回 `float`，表示计算得到的数值结果。
    """
    expected = _query_memory_type(query)
    return 1.0 if expected is not None and memory.memory_type == expected else 0.0


def _recency_score(memory: MemoryRecord) -> float:
    """函数功能：`_recency_score` 负责评分 recency，服务于本文件职责：长期记忆检索。
    传参：
        memory: memory 参数，由调用方传入，类型为 `MemoryRecord`。
    返回结果说明：
        返回 `float`，表示计算得到的数值结果。
    """
    updated = _parse_ts(memory.updated_at)
    if updated is None:
        return 0.0
    age_days = max(0.0, (datetime.now().astimezone() - updated).total_seconds() / 86400)
    if age_days <= 7:
        return 1.0
    if age_days >= 365:
        return 0.1
    return max(0.1, 1.0 - age_days / 365)


def score_memory(query: str, memory: MemoryRecord) -> float:
    """函数功能：`score_memory` 负责评分 memory，服务于本文件职责：长期记忆检索。
    传参：
        query: 检索或查询文本，类型为 `str`。
        memory: memory 参数，由调用方传入，类型为 `MemoryRecord`。
    返回结果说明：
        返回 `float`，表示计算得到的数值结果。
    """
    query_polarity = "unknown"
    memory_polarity = "unknown"
    if memory.memory_type == "preference":
        query_polarity = preference_query_polarity(query)
        memory_polarity = memory.polarity or preference_polarity(memory.content)
        if query_polarity == "positive" and memory_polarity == "negative":
            return 0.0
        if query_polarity == "negative" and memory_polarity == "positive":
            return 0.0

    searchable_fields = (
        memory.content,
        memory.object_value or "",
        memory.subject or "",
        memory.predicate or "",
        memory.memory_key or "",
    )
    topic_similarity = max(_overlap_score(query, value) for value in searchable_fields)
    intent_similarity = _intent_score(query, memory)
    if (
        memory.memory_type == "preference"
        and query_polarity in {"positive", "negative"}
        and query_polarity == memory_polarity
        and intent_similarity > 0
    ):
        # “我不喜欢喝什么”会刻意省略具体对象；保留 polarity 和 type 匹配，让相关负偏好越过检索阈值，同时避免相反偏好被误命中。
        topic_similarity = max(topic_similarity, 0.45)
    if topic_similarity <= 0 and intent_similarity <= 0:
        return 0.0

    status_factor = 1.0
    expected_type = _query_memory_type(query)
    if expected_type is not None and memory.memory_type != expected_type:
        # 明确结构化意图是确定性约束，不是软语义偏好；为不完美表达保留小 fallback 分，但不能让同标签事实排在目标任务前。
        status_factor *= 0.35
    if memory.status == "conflicted":
        status_factor = 0.5
    elif memory.status != "active":
        status_factor = 0.2
    if (
        memory.memory_type == "task"
        and memory.task_status in {"done", "cancelled"}
        and any(marker in query for marker in ("待办", "要做", "未完成", "还要"))
        and not any(marker in query for marker in ("状态", "完成", "取消", "做完"))
    ):
        status_factor *= 0.7

    access_frequency = min(memory.access_count / 10, 1.0)
    final = (
        0.72 * topic_similarity
        + 0.08 * intent_similarity
        + 0.07 * memory.importance
        + 0.05 * _recency_score(memory)
        + 0.05 * memory.confidence
        + 0.03 * access_frequency
    )
    return round(final * status_factor, 4)


def search(
    space_id: str,
    query: str,
    *,
    memory_type: str | None = None,
    min_score: float = MEMORY_QUERY_MIN_SCORE,
    limit: int = 10,
) -> list[dict[str, object]]:
    """函数功能：`search` 负责搜索，服务于本文件职责：长期记忆检索。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        query: 检索或查询文本，类型为 `str`。
        memory_type: memory type 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        min_score: min score 参数，由调用方传入，类型为 `float`，默认值为 `MEMORY_QUERY_MIN_SCORE`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `10`。
    返回结果说明：
        返回 `list[dict[str, object]]`，表示按条件筛选、构造或查询得到的列表。
    """
    from memory.repository import search_memories

    return [
        {
            **memory.to_dict(),
            "score": score,
        }
        for memory, score in search_memories(space_id, query, memory_type=memory_type, min_score=min_score, limit=limit)
    ]
