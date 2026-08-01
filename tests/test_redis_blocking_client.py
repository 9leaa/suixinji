"""文件作用：Redis blocking 客户端连接预算。

项目关系：本文件依赖 `infrastructure`、`infrastructure.redis_keys`、`runtime.streams.client`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from __future__ import annotations

from redis.exceptions import ResponseError

from infrastructure import redis_client
from infrastructure.redis_keys import RedisKeys
from runtime.streams.client import StreamClient


class _FakePool:
    """类功能：`_FakePool` 封装与“Redis blocking 客户端连接预算”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(self, kwargs: dict) -> None:
        """函数功能：`_FakePool.__init__` 在类 `_FakePool` 中负责初始化实例状态，服务于本文件职责：Redis blocking 客户端连接预算。
        传参：
            kwargs: kwargs 参数，由调用方传入，类型为 `dict`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.kwargs = kwargs
        self.disconnected = False

    def disconnect(self) -> None:
        """函数功能：`_FakePool.disconnect` 在类 `_FakePool` 中负责处理 disconnect，服务于本文件职责：Redis blocking 客户端连接预算。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.disconnected = True


class _FakeRedis:
    """类功能：`_FakeRedis` 封装与“Redis blocking 客户端连接预算”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(self, connection_pool: _FakePool | None = None, *, responses: list | None = None) -> None:
        """函数功能：`_FakeRedis.__init__` 在类 `_FakeRedis` 中负责初始化实例状态，服务于本文件职责：Redis blocking 客户端连接预算。
        传参：
            connection_pool: connection pool 参数，由调用方传入，类型为 `_FakePool | None`，默认值为 `None`。
            responses: responses 参数，由调用方传入，类型为 `list | None`，默认值为 `None`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.connection_pool = connection_pool
        self.responses = list(responses or [])
        self.closed = False
        self.xgroup_create_calls: list[tuple[str, str]] = []
        self.xreadgroup_calls: list[tuple[str, str, dict, int | None]] = []

    def close(self) -> None:
        """函数功能：`_FakeRedis.close` 在类 `_FakeRedis` 中负责关闭，服务于本文件职责：Redis blocking 客户端连接预算。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.closed = True

    def xgroup_create(self, stream: str, group: str, **_kwargs) -> None:
        """函数功能：`_FakeRedis.xgroup_create` 在类 `_FakeRedis` 中负责创建 xgroup，服务于本文件职责：Redis blocking 客户端连接预算。
        传参：
            stream: stream 参数，由调用方传入，类型为 `str`。
            group: group 参数，由调用方传入，类型为 `str`。
            **_kwargs:  kwargs 参数，由调用方传入。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.xgroup_create_calls.append((stream, group))

    def xreadgroup(self, group: str, consumer: str, streams: dict, *, count: int, block: int):
        """函数功能：`_FakeRedis.xreadgroup` 在类 `_FakeRedis` 中负责处理 xreadgroup，服务于本文件职责：Redis blocking 客户端连接预算。
        传参：
            group: group 参数，由调用方传入，类型为 `str`。
            consumer: consumer 参数，由调用方传入，类型为 `str`。
            streams: streams 参数，由调用方传入，类型为 `dict`。
            count: count 参数，由调用方传入，类型为 `int`。
            block: block 参数，由调用方传入，类型为 `int`。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
        self.xreadgroup_calls.append((group, consumer, streams, block))
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        stream = next(iter(streams))
        return [(stream, [("1-0", {"task_id": "task-blocking"})])]


def test_blocking_redis_has_independent_timeout_and_pool(monkeypatch) -> None:
    """函数功能：`test_blocking_redis_has_independent_timeout_and_pool` 负责验证 blocking redis has independent timeout and pool 场景，服务于本文件职责：Redis blocking 客户端连接预算。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    redis_client.close_redis()
    pools: list[_FakePool] = []

    def fake_from_url(_url: str, **kwargs) -> _FakePool:
        """函数功能：`fake_from_url` 负责处理 fake from url，服务于本文件职责：Redis blocking 客户端连接预算。
        传参：
            _url:  url 参数，由调用方传入，类型为 `str`。
            **kwargs: kwargs 参数，由调用方传入。
        返回结果说明：
            返回 `_FakePool` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
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
    """函数功能：`test_stream_read_uses_blocking_client_for_xreadgroup` 负责验证 stream read uses blocking client for xreadgroup 场景，服务于本文件职责：Redis blocking 客户端连接预算。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`test_stream_read_recovers_nogroup_with_blocking_client` 负责验证 stream read recovers nogroup with blocking client 场景，服务于本文件职责：Redis blocking 客户端连接预算。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
