"""文件作用：分布式锁归属、TTL 和释放边界。

项目关系：本文件依赖 `infrastructure`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from __future__ import annotations

from contextlib import contextmanager

import pytest

from infrastructure import redis_lock


class FakeRedisLock:
    """类功能：`FakeRedisLock` 封装与“分布式锁归属、TTL 和释放边界”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    ttl_ms = 30_000

    def __init__(self, *, acquire_result: bool = True, acquire_error: Exception | None = None, release_error: Exception | None = None) -> None:
        """函数功能：`FakeRedisLock.__init__` 在类 `FakeRedisLock` 中负责初始化实例状态，服务于本文件职责：分布式锁归属、TTL 和释放边界。
        传参：
            acquire_result: acquire result 参数，由调用方传入，类型为 `bool`，默认值为 `True`。
            acquire_error: acquire error 参数，由调用方传入，类型为 `Exception | None`，默认值为 `None`。
            release_error: release error 参数，由调用方传入，类型为 `Exception | None`，默认值为 `None`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.acquire_result = acquire_result
        self.acquire_error = acquire_error
        self.release_error = release_error
        self.acquire_calls = 0
        self.release_calls = 0
        self.renew_calls = 0

    def acquire(self, wait_seconds: float) -> bool:
        """函数功能：`FakeRedisLock.acquire` 在类 `FakeRedisLock` 中负责处理 acquire，服务于本文件职责：分布式锁归属、TTL 和释放边界。
        传参：
            wait_seconds: wait seconds 参数，由调用方传入，类型为 `float`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        del wait_seconds
        self.acquire_calls += 1
        if self.acquire_error is not None:
            raise self.acquire_error
        return self.acquire_result

    def renew(self) -> bool:
        """函数功能：`FakeRedisLock.renew` 在类 `FakeRedisLock` 中负责处理 renew，服务于本文件职责：分布式锁归属、TTL 和释放边界。
        传参：
            无。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        self.renew_calls += 1
        return True

    def release(self) -> bool:
        """函数功能：`FakeRedisLock.release` 在类 `FakeRedisLock` 中负责释放，服务于本文件职责：分布式锁归属、TTL 和释放边界。
        传参：
            无。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        self.release_calls += 1
        if self.release_error is not None:
            raise self.release_error
        return True


def _install(monkeypatch: pytest.MonkeyPatch, fake_lock: FakeRedisLock) -> list[str]:
    """函数功能：`_install` 负责处理 install，服务于本文件职责：分布式锁归属、TTL 和释放边界。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入，类型为 `pytest.MonkeyPatch`。
        fake_lock: fake lock 参数，由调用方传入，类型为 `FakeRedisLock`。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
    postgres_entries: list[str] = []
    monkeypatch.setattr(redis_lock, "COORDINATION_BACKEND", "redis")
    monkeypatch.setattr(redis_lock, "RedisDistributedLock", lambda _key: fake_lock)
    monkeypatch.setattr(redis_lock, "log_event", lambda *args, **kwargs: None)

    @contextmanager
    def fake_postgres_advisory_lock(key: str):
        """函数功能：`fake_postgres_advisory_lock` 负责加锁 fake postgres advisory，服务于本文件职责：分布式锁归属、TTL 和释放边界。
        传参：
            key: key 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        postgres_entries.append(key)
        yield

    monkeypatch.setattr(redis_lock, "postgres_advisory_lock", fake_postgres_advisory_lock)
    return postgres_entries


def test_coordinated_lock_redis_success_business_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """函数功能：`test_coordinated_lock_redis_success_business_success` 负责验证 coordinated lock redis success business success 场景，服务于本文件职责：分布式锁归属、TTL 和释放边界。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入，类型为 `pytest.MonkeyPatch`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    fake_lock = FakeRedisLock(acquire_result=True)
    postgres_entries = _install(monkeypatch, fake_lock)

    with redis_lock.coordinated_lock("lock:test") as backend:
        assert backend == "redis"

    assert fake_lock.acquire_calls == 1
    assert fake_lock.release_calls == 1
    assert postgres_entries == []


def test_coordinated_lock_redis_success_business_exception_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """函数功能：`test_coordinated_lock_redis_success_business_exception_propagates` 负责验证 coordinated lock redis success business exception propagates 场景，服务于本文件职责：分布式锁归属、TTL 和释放边界。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入，类型为 `pytest.MonkeyPatch`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    fake_lock = FakeRedisLock(acquire_result=True)
    postgres_entries = _install(monkeypatch, fake_lock)
    original = RuntimeError("business failed")

    with pytest.raises(RuntimeError) as exc_info:
        with redis_lock.coordinated_lock("lock:test") as backend:
            assert backend == "redis"
            raise original

    assert exc_info.value is original
    assert fake_lock.release_calls == 1
    assert postgres_entries == []


def test_coordinated_lock_redis_acquire_failure_falls_back_to_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    """函数功能：`test_coordinated_lock_redis_acquire_failure_falls_back_to_postgres` 负责验证 coordinated lock redis acquire failure falls back to postgres 场景，服务于本文件职责：分布式锁归属、TTL 和释放边界。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入，类型为 `pytest.MonkeyPatch`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    fake_lock = FakeRedisLock(acquire_error=ConnectionError("redis down"))
    postgres_entries = _install(monkeypatch, fake_lock)

    with redis_lock.coordinated_lock("lock:test") as backend:
        assert backend == "postgres"

    assert fake_lock.acquire_calls == 1
    assert fake_lock.release_calls == 0
    assert postgres_entries == ["lock:test"]


def test_coordinated_lock_non_critical_acquire_timeout_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """函数功能：`test_coordinated_lock_non_critical_acquire_timeout_does_not_fallback` 负责验证 coordinated lock non critical acquire timeout does not fallback 场景，服务于本文件职责：分布式锁归属、TTL 和释放边界。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入，类型为 `pytest.MonkeyPatch`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    fake_lock = FakeRedisLock(acquire_result=False)
    postgres_entries = _install(monkeypatch, fake_lock)

    with pytest.raises(TimeoutError):
        with redis_lock.coordinated_lock("lock:test", critical=False, wait_seconds=0):
            raise AssertionError("body must not run")

    assert fake_lock.acquire_calls == 1
    assert fake_lock.release_calls == 0
    assert postgres_entries == []


def test_coordinated_lock_release_failure_is_logged_and_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """函数功能：`test_coordinated_lock_release_failure_is_logged_and_does_not_fallback` 负责验证 coordinated lock release failure is logged and does not fallback 场景，服务于本文件职责：分布式锁归属、TTL 和释放边界。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入，类型为 `pytest.MonkeyPatch`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    fake_lock = FakeRedisLock(acquire_result=True, release_error=ConnectionError("release failed"))
    postgres_entries = _install(monkeypatch, fake_lock)
    events: list[dict[str, object]] = []
    monkeypatch.setattr(redis_lock, "log_event", lambda action, **kwargs: events.append({"action": action, **kwargs}))

    with redis_lock.coordinated_lock("lock:test") as backend:
        assert backend == "redis"

    assert fake_lock.release_calls == 1
    assert postgres_entries == []
    assert events[-1]["action"] == "runtime.lock_release_failed"
    assert events[-1]["status"] == "degraded"


def test_coordinated_lock_business_exception_does_not_double_yield(monkeypatch: pytest.MonkeyPatch) -> None:
    """函数功能：`test_coordinated_lock_business_exception_does_not_double_yield` 负责验证 coordinated lock business exception does not double yield 场景，服务于本文件职责：分布式锁归属、TTL 和释放边界。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入，类型为 `pytest.MonkeyPatch`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    fake_lock = FakeRedisLock(acquire_result=True)
    _install(monkeypatch, fake_lock)

    @contextmanager
    def forbidden_postgres_advisory_lock(_key: str):
        """函数功能：`forbidden_postgres_advisory_lock` 负责加锁 forbidden postgres advisory，服务于本文件职责：分布式锁归属、TTL 和释放边界。
        传参：
            _key:  key 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        raise AssertionError("business exceptions must not fallback")
        yield

    monkeypatch.setattr(redis_lock, "postgres_advisory_lock", forbidden_postgres_advisory_lock)

    with pytest.raises(ValueError) as exc_info:
        with redis_lock.coordinated_lock("lock:test"):
            raise ValueError("original business error")

    assert str(exc_info.value) == "original business error"
    assert "generator didn't stop after throw" not in str(exc_info.value)
