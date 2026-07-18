from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from infrastructure.redis_cache import RedisCache
from infrastructure.redis_client import get_redis
from infrastructure.redis_idempotency import IdempotencyStore
from infrastructure.redis_keys import RedisKeys
from infrastructure.redis_lock import RedisDistributedLock
from infrastructure.redis_rate_limit import RedisRateLimiter
from infrastructure.redis_session import RedisSessionStore

pytestmark = pytest.mark.skipif(not os.getenv("REDIS_URL"), reason="Redis integration URL is not configured")


@pytest.fixture
def redis_namespace():
    client = get_redis()
    keys = RedisKeys(env=f"test-{uuid.uuid4().hex}")
    yield client, keys
    for key in client.scan_iter(match=f"{keys.prefix}:*"):
        client.delete(key)


def test_rate_limit_is_shared_and_user_isolated(redis_namespace):
    client, keys = redis_namespace
    limiter = RedisRateLimiter(client)
    first_user = keys.rate_user("t1", "u1", "ask")
    second_user = keys.rate_user("t1", "u2", "ask")
    assert limiter.allow(first_user, 2, window_seconds=1).allowed
    assert limiter.allow(first_user, 2, window_seconds=1).allowed
    assert not limiter.allow(first_user, 2, window_seconds=1).allowed
    assert limiter.allow(second_user, 2, window_seconds=1).allowed
    time.sleep(1.05)
    assert limiter.allow(first_user, 2, window_seconds=1).allowed


def test_idempotency_has_single_concurrent_winner(redis_namespace):
    client, keys = redis_namespace
    store = IdempotencyStore(client, ttl_seconds=10)
    key = keys.idempotency("t1", "test", "message")
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _index: store.begin(key), range(10)))
    assert results.count(True) == 1
    store.complete(key)
    assert store.get(key) == "completed"


def test_lock_token_cache_version_and_session_ttl(redis_namespace):
    client, keys = redis_namespace
    first = RedisDistributedLock(keys.lock_space("t1", "s1"), client=client, ttl_ms=1000)
    second = RedisDistributedLock(keys.lock_space("t1", "s1"), client=client, ttl_ms=1000)
    assert first.acquire(wait_seconds=0)
    assert not second.acquire(wait_seconds=0)
    assert not second.release()
    assert first.renew()
    assert first.release()
    assert second.acquire(wait_seconds=0)
    second.release()

    cache = RedisCache(client, keys=keys)
    cache.set("memory_search", "s1", "query", [{"id": "m1"}], ttl_seconds=10, tenant_id="t1")
    assert cache.get("memory_search", "s1", "query", tenant_id="t1") == [{"id": "m1"}]
    cache.bump_version("s1", tenant_id="t1")
    assert cache.get("memory_search", "s1", "query", tenant_id="t1") is None

    sessions = RedisSessionStore(client, ttl_seconds=1, keys=keys)
    sessions.set("t1", "u1", {"waiting_for": "range"})
    sessions.set("t1", "u2", {"waiting_for": "title"})
    assert sessions.get("t1", "u1")["waiting_for"] == "range"
    assert sessions.get("t1", "u2")["waiting_for"] == "title"
    time.sleep(1.05)
    assert sessions.get("t1", "u1") == {}


def test_business_redis_keys_are_tenant_isolated():
    keys = RedisKeys(env="test-stage4")
    assert keys.rate_user("tenant-a", "same-user", "ask") != keys.rate_user("tenant-b", "same-user", "ask")
    assert keys.idempotency("tenant-a", "api", "same-message") != keys.idempotency("tenant-b", "api", "same-message")
    assert keys.lock_space("tenant-a", "same-space") != keys.lock_space("tenant-b", "same-space")
    assert keys.cache_search("tenant-a", "memory", "same-space", 1, "query") != keys.cache_search("tenant-b", "memory", "same-space", 1, "query")
