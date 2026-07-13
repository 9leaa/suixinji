"""Central runtime settings for Suixinji."""

from __future__ import annotations

import os


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return float(value)


MAX_WORKERS = _int_env("SUIXINJI_MAX_WORKERS", 4)
TASK_QUEUE_SIZE = _int_env("SUIXINJI_TASK_QUEUE_SIZE", 100)
TASK_HISTORY_LIMIT = _int_env("SUIXINJI_TASK_HISTORY_LIMIT", 1000)
TASK_HISTORY_TTL_HOURS = _int_env("SUIXINJI_TASK_HISTORY_TTL_HOURS", 24)
PENDING_DRAIN_INTERVAL_SECONDS = _int_env("SUIXINJI_PENDING_DRAIN_INTERVAL_SECONDS", 15)
PENDING_DRAIN_BATCH_SIZE = _int_env("SUIXINJI_PENDING_DRAIN_BATCH_SIZE", 20)
LLM_TIMEOUT_SECONDS = _int_env("SUIXINJI_LLM_TIMEOUT_SECONDS", 30)
LLM_MAX_RETRIES = _int_env("SUIXINJI_LLM_MAX_RETRIES", 2)
EMBEDDING_TIMEOUT_SECONDS = _int_env("SUIXINJI_EMBEDDING_TIMEOUT_SECONDS", 20)

RELATED_TOP_K = _int_env("SUIXINJI_RELATED_TOP_K", 3)
RELATED_MIN_SCORE = _float_env("SUIXINJI_RELATED_MIN_SCORE", 0.5)
QUERY_TOP_K = _int_env("SUIXINJI_QUERY_TOP_K", 5)
QUERY_MIN_SCORE = _float_env("SUIXINJI_QUERY_MIN_SCORE", 0.55)

SUMMARY_DEFAULT_TIME = os.getenv("SUIXINJI_SUMMARY_DEFAULT_TIME", "22:00")
