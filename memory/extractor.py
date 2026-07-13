"""Deterministic first-pass memory extraction."""

from __future__ import annotations

import re
from typing import Any

from memory.models import MemoryCandidate

LOW_VALUE_PATTERNS = {
    "你好",
    "hello",
    "hi",
    "收到",
    "好的",
    "ok",
    "哈哈",
    "嗯",
    "嗯嗯",
    "今天天气不错",
}

SENSITIVE_HINTS = ("身份证", "银行卡", "密码", "api key", "apikey", "secret", "token")
LOW_CONFIDENCE_HINTS = ("可能", "也许", "大概", "好像", "猜一下")


def _entities(text: str) -> list[str]:
    found = re.findall(r"[A-Za-z][A-Za-z0-9+#.-]*", text)
    for keyword in ("咖啡", "苹果", "北京", "上海", "Java", "Python", "Agent", "RAG", "README", "CI"):
        if keyword in text and keyword not in found:
            found.append(keyword)
    return found[:8]


def _task_status(text: str) -> str:
    if any(token in text for token in ("取消", "不用做", "不做了")):
        return "cancelled"
    if any(token in text for token in ("完成", "搞定", "已做完", "做完")):
        return "done"
    if any(token in text for token in ("卡住", "阻塞", "等确认")):
        return "blocked"
    if any(token in text for token in ("正在", "进行中", "继续")):
        return "in_progress"
    return "todo"


def _clean_subject(text: str) -> str:
    value = text.strip()
    value = re.sub(r"^(记得|需要|待办|todo[:：]?|帮我记一下|提醒我)", "", value, flags=re.IGNORECASE).strip(" ：:")
    return value or text.strip()


def extract_candidates(note_id: str, text: str, classification: dict[str, Any] | None = None) -> list[MemoryCandidate]:
    """Extract long-term memory candidates without calling external APIs."""
    del note_id
    del classification
    raw = str(text or "").strip()
    compact = re.sub(r"\s+", "", raw).casefold()
    if not raw:
        return []
    if compact in LOW_VALUE_PATTERNS or len(compact) <= 2:
        return []
    if any(token in compact for token in SENSITIVE_HINTS):
        return []
    if any(token in raw for token in LOW_CONFIDENCE_HINTS) and "记住" not in raw:
        return []

    entities = _entities(raw)
    candidates: list[MemoryCandidate] = []

    preference_markers = ("喜欢", "不喜欢", "更喜欢", "偏好", "习惯", "不想", "不打算", "暂时不", "只学", "重点放在", "过敏")
    task_markers = ("记得", "需要", "待办", "todo", "跟进", "修", "改", "实现", "完成", "提醒")
    semantic_markers = ("正在", "重点", "学习", "研究", "开发", "负责", "住在", "搬到", "使用", "采用")

    if any(marker in raw for marker in preference_markers):
        candidates.append(
            MemoryCandidate(
                memory_type="preference",
                content=f"用户{_clean_subject(raw)}",
                importance=0.75,
                confidence=0.86,
                entities=entities,
                reason="preference_marker",
            )
        )

    if any(marker in raw.casefold() for marker in task_markers):
        candidates.append(
            MemoryCandidate(
                memory_type="task",
                content=_clean_subject(raw),
                importance=0.8,
                confidence=0.82,
                entities=entities,
                task_status=_task_status(raw),
                reason="task_marker",
            )
        )

    if any(marker in raw for marker in semantic_markers):
        candidates.append(
            MemoryCandidate(
                memory_type="semantic",
                content=f"用户{_clean_subject(raw)}",
                importance=0.78,
                confidence=0.84,
                entities=entities,
                reason="semantic_marker",
            )
        )

    if not candidates and len(raw) >= 12 and any(marker in raw for marker in ("今天", "昨天", "刚才", "完成了", "去了", "参加", "发布")):
        candidates.append(
            MemoryCandidate(
                memory_type="episodic",
                content=raw,
                importance=0.55,
                confidence=0.72,
                entities=entities,
                reason="episodic_event",
            )
        )

    deduped: list[MemoryCandidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (candidate.memory_type, candidate.normalized_content)
        if key not in seen:
            deduped.append(candidate)
            seen.add(key)
    return deduped
