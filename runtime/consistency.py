"""Deterministic consistency requirements for distributed root tasks."""

from __future__ import annotations

from typing import Any


MEMORY_QUERY_MARKERS = (
    "喜欢",
    "讨厌",
    "偏好",
    "习惯",
    "过敏",
    "当前待办",
    "现在的任务",
    "待办是什么",
    "任务进度",
    "住在哪里",
    "住哪",
    "现在住",
    "正在学习",
    "当前项目",
    "what do i like",
    "preference",
    "todo",
    "current task",
    "where do i live",
)
NOTE_QUERY_MARKERS = (
    "最近",
    "笔记",
    "记录",
    "记了",
    "写了",
    "/type",
    "/tag",
    "标签",
    "类型",
    "recent",
    "record",
    "note",
)


def query_consistency(question: str) -> str:
    normalized = " ".join(str(question or "").strip().casefold().split())
    if any(marker in normalized for marker in MEMORY_QUERY_MARKERS):
        return "memory"
    if any(marker in normalized for marker in NOTE_QUERY_MARKERS):
        return "note"
    return "weak"


def task_consistency(task_type: str, payload: dict[str, Any]) -> str:
    explicit = str(payload.get("consistency") or "").strip().lower()
    if explicit in {"note", "memory", "weak"}:
        return explicit
    if task_type == "ingest" or task_type == "summary":
        return "note"
    if task_type == "query":
        return query_consistency(str(payload.get("question") or ""))
    return "weak"
