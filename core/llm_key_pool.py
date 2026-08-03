"""Thread-safe API-key pools for OpenAI-compatible providers.

The pool is intentionally provider-agnostic.  It only chooses credentials and
tracks temporary cooldowns; request construction and response handling remain
in :mod:`core.llm_client`.
"""

from __future__ import annotations

import os
import random
import re
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any


def _split_keys(value: str | None) -> list[str]:
    if not value:
        return []
    value = value.strip()
    if not value:
        return []
    # Permit comma, semicolon, whitespace, or one key per line.  Keys are
    # treated as opaque strings and are never written to logs.
    return [item for item in re.split(r"[,;\s]+", value) if item]


def _configured_keys(kind: str, fallback: str | None = None) -> list[str]:
    scoped_name = "SUIXINJI_LLM_API_KEYS" if kind == "chat" else "SUIXINJI_EMBEDDING_API_KEYS"
    values: list[str] = []
    values.extend(_split_keys(os.getenv(scoped_name)))
    if kind == "chat":
        values.extend(_split_keys(os.getenv("OPENAI_API_KEYS")))
    else:
        values.extend(_split_keys(os.getenv("DASHSCOPE_API_KEYS")))

    # Keep chat and embedding pools isolated.  Deployments often use numbered
    # OPENAI_API_KEY_* values for chat failover while using one DashScope (or
    # another OpenAI-compatible) embedding provider.  Treating chat keys as
    # embedding keys makes the embedding pool intermittently pick credentials
    # that are invalid for the embedding base URL.
    numbered_prefixes = ("OPENAI_API_KEY_",) if kind == "chat" else (
        "DASHSCOPE_API_KEY_",
        "SUIXINJI_EMBEDDING_API_KEY_",
        "EMBEDDING_API_KEY_",
    )
    numbered: list[tuple[int, str]] = []
    for name, value in os.environ.items():
        for prefix in numbered_prefixes:
            if not name.startswith(prefix) or name == prefix:
                continue
            suffix = name[len(prefix) :]
            if suffix.isdigit() and value.strip():
                numbered.append((int(suffix), value.strip()))
                break
    values.extend(value for _number, value in sorted(numbered))
    values.extend(_split_keys(fallback))

    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            unique.append(value)
            seen.add(value)
    return unique


def retry_after_seconds(exc: BaseException) -> float | None:
    """Read Retry-After from an SDK exception or its message."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                try:
                    target = parsedate_to_datetime(str(raw)).timestamp()
                    return max(0.0, target - time.time())
                except Exception:
                    pass
    text = str(exc)
    match = re.search(r"retry\s+after\s+(\d+(?:\.\d+)?)\s*(ms|s|seconds?)?", text, re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    return value / 1000.0 if (match.group(2) or "").lower() == "ms" else value


@dataclass
class _KeyState:
    key: str
    cooldown_until: float = 0.0
    failures: int = 0
    successes: int = 0


class APIKeyPool:
    """Round-robin key pool with per-key cooldowns.

    ``acquire`` returns a key and a suggested wait.  The caller may pass an
    exclusion set so a failed key is not immediately retried while another
    configured key is available.
    """

    def __init__(self, kind: str, keys: list[str]):
        self.kind = kind
        self._states = [_KeyState(key) for key in keys]
        self._cursor = 0
        self._lock = threading.RLock()
        self.default_cooldown_seconds = max(1.0, float(os.getenv("SUIXINJI_LLM_KEY_COOLDOWN_SECONDS", "30")))
        self.failure_cooldown_seconds = max(0.0, float(os.getenv("SUIXINJI_LLM_KEY_FAILURE_COOLDOWN_SECONDS", "2")))

    @property
    def size(self) -> int:
        return len(self._states)

    def slot(self, key: str | None) -> int | None:
        if not key:
            return None
        with self._lock:
            for index, state in enumerate(self._states, 1):
                if state.key == key:
                    return index
        return None

    def acquire(self, *, exclude: set[str] | None = None) -> tuple[str | None, float]:
        with self._lock:
            if not self._states:
                return None, 0.0
            exclude = exclude or set()
            now = time.monotonic()
            ordered = [(self._cursor + offset) % len(self._states) for offset in range(len(self._states))]
            candidates = [index for index in ordered if self._states[index].key not in exclude]
            # If every key was used in this request, allow reuse after all
            # alternatives have been attempted (important for a single-key
            # deployment and for timeout retries).
            if not candidates:
                candidates = ordered
            available = [index for index in candidates if self._states[index].cooldown_until <= now]
            if available:
                index = available[0]
                self._cursor = (index + 1) % len(self._states)
                return self._states[index].key, 0.0
            earliest = min(self._states[index].cooldown_until for index in candidates)
            return None, max(0.0, earliest - now)

    def report_success(self, key: str | None) -> None:
        if not key:
            return
        with self._lock:
            for state in self._states:
                if state.key == key:
                    state.successes += 1
                    state.failures = 0
                    return

    def report_failure(self, key: str | None, *, category: str, retry_after: float | None = None) -> None:
        if not key:
            return
        with self._lock:
            for state in self._states:
                if state.key != key:
                    continue
                state.failures += 1
                if category == "rate_limit":
                    cooldown = retry_after if retry_after is not None else self.default_cooldown_seconds
                    # Avoid synchronized retries when several workers receive
                    # the same provider 429 at once.
                    jitter = min(0.25, max(0.0, float(os.getenv("SUIXINJI_LLM_RETRY_JITTER", "0.10"))))
                    cooldown *= 1.0 + random.uniform(-jitter, jitter)
                    state.cooldown_until = max(state.cooldown_until, time.monotonic() + max(0.0, cooldown))
                elif category in {"connection_error", "server_error"} and self.failure_cooldown_seconds:
                    state.cooldown_until = max(
                        state.cooldown_until,
                        time.monotonic() + self.failure_cooldown_seconds,
                    )
                return

    def has_alternative(self, used: set[str]) -> bool:
        with self._lock:
            now = time.monotonic()
            return any(state.key not in used and state.cooldown_until <= now for state in self._states)


_POOLS: dict[tuple[str, tuple[str, ...]], APIKeyPool] = {}
_POOLS_LOCK = threading.RLock()


def get_api_key_pool(kind: str, fallback: str | None = None) -> APIKeyPool:
    keys = tuple(_configured_keys(kind, fallback))
    cache_key = (kind, keys)
    with _POOLS_LOCK:
        pool = _POOLS.get(cache_key)
        if pool is None:
            pool = APIKeyPool(kind, list(keys))
            _POOLS[cache_key] = pool
        return pool


def pool_stats() -> dict[str, Any]:
    """Return non-sensitive diagnostics for health endpoints and tests."""
    with _POOLS_LOCK:
        result: dict[str, Any] = {}
        for (kind, _keys), pool in _POOLS.items():
            with pool._lock:
                result[kind] = {
                    "key_count": pool.size,
                    "cooling_slots": sum(1 for state in pool._states if state.cooldown_until > time.monotonic()),
                    "successes": sum(state.successes for state in pool._states),
                    "failures": sum(state.failures for state in pool._states),
                }
        return result
