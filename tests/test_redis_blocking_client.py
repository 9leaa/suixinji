from __future__ import annotations

from redis.exceptions import ResponseError

from infrastructure import redis_client
from infrastructure.redis_keys import RedisKeys
from runtime.streams.client import StreamClient


class _FakePool:
    def __init__(self, kwargs: dict) -> None:
        """初始化`_FakePool` 实例并建立后续调用所需的状态。"""
        self.kwargs = kwargs
        self.disconnected = False

    def disconnect(self) -> None:
        """验证“disconnect”场景的预期行为与回归边界。"""
        self.disconnected = True


class _FakeRedis:
    def __init__(self, connection_pool: _FakePool | None = None, *, responses: list | None = None) -> None:
        """初始化`_FakeRedis` 实例并建立后续调用所需的状态。"""
        self.connection_pool = connection_pool
        self.responses = list(responses or [])
        self.closed = False
        self.xgroup_create_calls: list[tuple[str, str]] = []
        self.xreadgroup_calls: list[tuple[str, str, dict, int | None]] = []

    def close(self) -> None:
        """验证“close”场景的预期行为与回归边界。"""
        self.closed = True

    def xgroup_create(self, stream: str, group: str, **_kwargs) -> None:
        """验证“xgroup创建”场景的预期行为与回归边界。"""
        self.xgroup_create_calls.append((stream, group))

    def xreadgroup(self, group: str, consumer: str, streams: dict, *, count: int, block: int):
        """验证“xreadgroup”场景的预期行为与回归边界。"""
        self.xreadgroup_calls.append((group, consumer, streams, block))
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        stream = next(iter(streams))
        return [(stream, [("1-0", {"task_id": "task-blocking"})])]


def test_blocking_redis_has_independent_timeout_and_pool(monkeypatch) -> None:
    """验证“blockingredis是否包含independent超时andpool”场景的预期行为与回归边界。"""
    redis_client.close_redis()
    pools: list[_FakePool] = []

    def fake_from_url(_url: str, **kwargs) -> _FakePool:
        """验证“fakefromurl”场景的预期行为与回归边界。"""
        pool = _FakePool(kwargs)
        pools.append(pool)
        return pool

    monkeypatch.setattr(redis_client, "REDIS_URL", "redis://example/0")
    monkeypatch.setattr(redis_client, "REDIS_MAX_CONNECTIONS", 20)
    monkeypatch.setattr(redis_client, "REDIS_BLOCKING_MAX_CONNECTIONS", 8)
    monkeypatch.setattr(redis_client, "REDIS_SOCKET_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(redis_client, "REDIS_BLOCKING_SOCKET_TIMEOUT_SECONDS", 7.0)
    monkeypatch.setattr(redis_client.ConnectionPool, "from_url", fake_from_url)
    monkeypatch.setattr(redis_client, "Redis", _FakeRedis)

    normal = redis_client.get_redis()
    blocking = redis_client.get_blocking_redis()

    assert normal is not blocking
    assert pools[0].kwargs["socket_timeout"] == 2.0
    assert pools[0].kwargs["max_connections"] == 20
    assert pools[1].kwargs["socket_timeout"] == 7.0
    assert pools[1].kwargs["max_connections"] == 8

    redis_client.close_redis()
    assert normal.closed
    assert blocking.closed
    assert all(pool.disconnected for pool in pools)


def test_stream_read_uses_blocking_client_for_xreadgroup() -> None:
    """验证“流读取usesblockingclientforxreadgroup”场景的预期行为与回归边界。"""
    keys = RedisKeys(env="test-blocking")
    stream = keys.stream("ingest")
    normal = _FakeRedis()
    blocking = _FakeRedis()
    client = StreamClient(normal, blocking_client=blocking, keys=keys)

    messages = client.read("ingest", "consumer-a", block_ms=5000)

    assert normal.xgroup_create_calls == [(stream, "ingest-workers")]
    assert normal.xreadgroup_calls == []
    assert blocking.xreadgroup_calls == [("ingest-workers", "consumer-a", {stream: ">"}, 5000)]
    assert messages[0].message_id == "1-0"
    assert messages[0].fields == {"task_id": "task-blocking"}


def test_stream_read_recovers_nogroup_with_blocking_client() -> None:
    """验证“流读取recoversnogroupwithblockingclient”场景的预期行为与回归边界。"""
    keys = RedisKeys(env="test-nogroup")
    stream = keys.stream("ingest")
    normal = _FakeRedis()
    blocking = _FakeRedis(
        responses=[
            ResponseError("NOGROUP No such key or consumer group"),
            [(stream, [("2-0", {"task_id": "task-after-nogroup"})])],
        ]
    )
    client = StreamClient(normal, blocking_client=blocking, keys=keys)

    messages = client.read("ingest", "consumer-a", block_ms=5000)

    assert normal.xgroup_create_calls == [
        (stream, "ingest-workers"),
        (stream, "ingest-workers"),
    ]
    assert len(blocking.xreadgroup_calls) == 2
    assert messages[0].message_id == "2-0"
    assert messages[0].fields == {"task_id": "task-after-nogroup"}
