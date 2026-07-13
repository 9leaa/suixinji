"""Retry helpers for transient external failures."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar


T = TypeVar("T")
BACKOFF_SECONDS = (1, 3)


def retry_external_call(
    fn: Callable[[], T],
    *,
    max_retries: int,
    retryable: Callable[[BaseException], bool],
) -> T:
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            if attempt >= max_retries or not retryable(exc):
                raise
            delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
            time.sleep(delay)
            attempt += 1
