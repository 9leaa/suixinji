"""Central Redis key builder; every key is namespaced by environment."""

from __future__ import annotations

import hashlib
from urllib.parse import quote

from core.settings import SUIXINJI_ENV


def _part(value: object) -> str:
    return quote(str(value or "unknown"), safe="-_.")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


class RedisKeys:
    def __init__(self, env: str = SUIXINJI_ENV) -> None:
        self.prefix = f"sxj:{_part(env)}"

    def rate_user(self, user_id: str, action: str) -> str:
        return f"{self.prefix}:rate:user:{_part(user_id)}:{_part(action)}"

    def rate_tenant_tokens(self, tenant_id: str) -> str:
        return f"{self.prefix}:rate:tenant:{_part(tenant_id)}:llm-tokens"

    def concurrency_llm(self, tenant_id: str) -> str:
        return f"{self.prefix}:concurrency:tenant:{_part(tenant_id)}:llm"

    def idempotency(self, source: str, message_id: str) -> str:
        return f"{self.prefix}:idem:message:{_part(source)}:{_part(message_id)}"

    def lock_space(self, space_id: str) -> str:
        return f"{self.prefix}:lock:space:{_part(space_id)}"

    def lock_memory(self, memory_id: str) -> str:
        return f"{self.prefix}:lock:memory:{_part(memory_id)}"

    def lock_memory_key(self, space_id: str, memory_key: str) -> str:
        return f"{self.prefix}:lock:memory-key:{_part(space_id)}:{_hash(memory_key)}"

    def lock_scheduler(self, job_name: str) -> str:
        return f"{self.prefix}:lock:scheduler:{_part(job_name)}"

    def cache_version(self, space_id: str) -> str:
        return f"{self.prefix}:cachever:space:{_part(space_id)}"

    def cache_search(self, kind: str, space_id: str, version: int, query_payload: str) -> str:
        return f"{self.prefix}:cache:{_part(kind)}:{_part(space_id)}:{version}:{_hash(query_payload)}"

    def cache_embedding(self, model: str, text: str) -> str:
        return f"{self.prefix}:cache:embedding:{_part(model)}:{_hash(text)}"

    def memory_access_counts(self) -> str:
        return f"{self.prefix}:memory:access:counts"

    def memory_access_last_seen(self) -> str:
        return f"{self.prefix}:memory:access:last-seen"

    def session(self, tenant_id: str, user_id: str) -> str:
        return f"{self.prefix}:session:{_part(tenant_id)}:{_part(user_id)}"

    def stream(self, task_type: str) -> str:
        return f"{self.prefix}:stream:{_part(task_type)}"

    def dead_letter_stream(self) -> str:
        return self.stream("deadletter")


KEYS = RedisKeys()
