"""Per-space file locks and filesystem-safe space IDs."""

from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from collections.abc import Iterator


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def safe_space_id(space_id: str) -> str:
    """Convert a space_id into a safe filesystem path fragment."""
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", str(space_id))


def get_space_lock(space_id: str) -> threading.RLock:
    """Return the process-local reentrant lock for a space_id."""
    key = safe_space_id(space_id)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


@contextmanager
def locked_space(space_id: str) -> Iterator[None]:
    """Serialize file reads/writes for one space_id within this process."""
    lock = get_space_lock(space_id)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
