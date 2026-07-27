"""Memory retrieval scoring."""

from __future__ import annotations

from datetime import datetime
import re

from core.settings import MEMORY_QUERY_MIN_SCORE
from memory.models import MemoryRecord, normalize_content
from memory.policies.preference import preference_polarity, preference_query_polarity


def _parse_ts(value: str | None) -> datetime | None:
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
    text = str(value or "").casefold()
    for filler in sorted(_QUERY_FILLERS, key=len, reverse=True):
        text = text.replace(filler, " ")
    text = re.sub(r"(?:我|用户)(?:现在|当前|最近)?", " ", text)
    text = re.sub(r"(?:不喜欢|喜欢|讨厌|偏好|习惯|要做|待办|任务|进度|完成|取消)", " ", text)
    text = re.sub(r"[的了呢吗么嘛呀啊吧和与及、，,。；;：:！？?!()（）\[\]{}]", " ", text)
    return " ".join(text.split())


def _named_tokens(value: str) -> set[str]:
    # CJK family identifiers (生活-02) are as meaningful as ASCII issue keys
    # (PROJ-123). Match the complete identifier so a shared numeric suffix
    # cannot make two unrelated records look equivalent.
    pattern = r"[\u4e00-\u9fffA-Za-z]+-\d+|[A-Za-z][A-Za-z0-9+#._-]*|\d+(?:[._-]\d+)*"
    return {token.casefold() for token in re.findall(pattern, value)}


def _cjk_ngrams(value: str) -> set[str]:
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
    """Topic relevance based on phrases/tokens, never isolated CJK chars."""
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
        # A shared generic token such as Agent or SQL is not enough when the
        # Chinese topic qualifier disagrees (Agent简历 vs Agent评测).
        return round(0.35 * named_score + 0.65 * cjk_score, 4)
    return round(named_score if q_named else cjk_score, 4)


def _query_memory_type(query: str) -> str | None:
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
    expected = _query_memory_type(query)
    return 1.0 if expected is not None and memory.memory_type == expected else 0.0


def _recency_score(memory: MemoryRecord) -> float:
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
        # "我不喜欢喝什么" intentionally omits the concrete object. Preserve
        # polarity and type matching so a relevant negative preference clears
        # the retrieval threshold without making an opposite preference match.
        topic_similarity = max(topic_similarity, 0.45)
    if topic_similarity <= 0 and intent_similarity <= 0:
        return 0.0

    status_factor = 1.0
    expected_type = _query_memory_type(query)
    if expected_type is not None and memory.memory_type != expected_type:
        # A clear structured intent is a deterministic constraint, not a soft
        # semantic preference. Keep a small fallback score for imperfect user
        # phrasing, but do not let a same-label fact outrank the requested task.
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
    from memory.repository import search_memories

    return [
        {
            **memory.to_dict(),
            "score": score,
        }
        for memory, score in search_memories(space_id, query, memory_type=memory_type, min_score=min_score, limit=limit)
    ]
