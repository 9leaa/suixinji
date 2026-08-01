"""文件作用：本地 space 文件锁。

项目关系：本文件依赖 无直接本地模块依赖；被 `core.feedback`、`core.wal`、`memory.scheduler`、`runtime.executor` 等 7 个模块。
"""



from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from collections.abc import Iterator


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def safe_space_id(space_id: str) -> str:
    """函数功能：`safe_space_id` 负责处理 safe space id，服务于本文件职责：本地 space 文件锁。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", str(space_id))


def get_space_lock(space_id: str) -> threading.RLock:
    """函数功能：`get_space_lock` 负责获取 space lock，服务于本文件职责：本地 space 文件锁。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `threading.RLock` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    key = safe_space_id(space_id)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


@contextmanager
def locked_space(space_id: str) -> Iterator[None]:
    """函数功能：`locked_space` 负责处理 locked space，服务于本文件职责：本地 space 文件锁。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `Iterator[None]` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    lock = get_space_lock(space_id)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
