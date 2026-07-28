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
    """负责“重试externalcall”。

    该函数是 `runtime.retry` 中的模块函数；具体输入、输出和异常边界由类型标注及调用方约定。
    """
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
