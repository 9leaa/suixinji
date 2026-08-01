"""文件作用：重试策略。

项目关系：本文件依赖 无直接本地模块依赖；被 `tests.test_retry_boundaries`。
"""



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
    """函数功能：`retry_external_call` 负责重试 external call，服务于本文件职责：重试策略。
    传参：
        fn: fn 参数，由调用方传入，类型为 `Callable[[], T]`。
        max_retries: max retries 参数，由调用方传入，类型为 `int`。
        retryable: retryable 参数，由调用方传入，类型为 `Callable[[BaseException], bool]`。
    返回结果说明：
        返回 `T` 类型结果；具体字段和语义由调用方按该对象约定使用。
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
