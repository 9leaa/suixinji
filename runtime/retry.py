"""Retry helpers for transient external failures."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from core.settings import LLM_MAX_RETRIES


T = TypeVar("T")
BACKOFF_SECONDS = (1, 3)


def is_retryable_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".casefold()
    retry_markers = (
        "timeout",
        "timed out",
        "rate limit",
        "429",
        "500",
        "502",
        "503",
        "504",
        "connection",
        "temporar",
        "network",
    )
    non_retry_markers = (
        "valueerror",
        "jsondecodeerror",
        "invalid json",
        "valid json object",
        "missing required environment",
        "configuration",
        "config",
    )
    if any(marker in text for marker in non_retry_markers):
        return False
    return any(marker in text for marker in retry_markers)


def run_with_retries(fn: Callable[[], T], *, max_retries: int = LLM_MAX_RETRIES) -> T:
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            if attempt >= max_retries or not is_retryable_error(exc):
                raise
            delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
            time.sleep(delay)
            attempt += 1
