from __future__ import annotations

from contextlib import contextmanager

import pytest

from infrastructure import redis_lock


class FakeRedisLock:
    ttl_ms = 30_000

    def __init__(self, *, acquire_result: bool = True, acquire_error: Exception | None = None, release_error: Exception | None = None) -> None:
        self.acquire_result = acquire_result
        self.acquire_error = acquire_error
        self.release_error = release_error
        self.acquire_calls = 0
        self.release_calls = 0
        self.renew_calls = 0

    def acquire(self, wait_seconds: float) -> bool:
        del wait_seconds
        self.acquire_calls += 1
        if self.acquire_error is not None:
            raise self.acquire_error
        return self.acquire_result

    def renew(self) -> bool:
        self.renew_calls += 1
        return True

    def release(self) -> bool:
        self.release_calls += 1
        if self.release_error is not None:
            raise self.release_error
        return True


def _install(monkeypatch: pytest.MonkeyPatch, fake_lock: FakeRedisLock) -> list[str]:
    postgres_entries: list[str] = []
    monkeypatch.setattr(redis_lock, "COORDINATION_BACKEND", "redis")
    monkeypatch.setattr(redis_lock, "RedisDistributedLock", lambda _key: fake_lock)
    monkeypatch.setattr(redis_lock, "log_event", lambda *args, **kwargs: None)

    @contextmanager
    def fake_postgres_advisory_lock(key: str):
        postgres_entries.append(key)
        yield

    monkeypatch.setattr(redis_lock, "postgres_advisory_lock", fake_postgres_advisory_lock)
    return postgres_entries


def test_coordinated_lock_redis_success_business_success(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_lock = FakeRedisLock(acquire_result=True)
    postgres_entries = _install(monkeypatch, fake_lock)

    with redis_lock.coordinated_lock("lock:test") as backend:
        assert backend == "redis"

    assert fake_lock.acquire_calls == 1
    assert fake_lock.release_calls == 1
    assert postgres_entries == []


def test_coordinated_lock_redis_success_business_exception_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
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
    fake_lock = FakeRedisLock(acquire_error=ConnectionError("redis down"))
    postgres_entries = _install(monkeypatch, fake_lock)

    with redis_lock.coordinated_lock("lock:test") as backend:
        assert backend == "postgres"

    assert fake_lock.acquire_calls == 1
    assert fake_lock.release_calls == 0
    assert postgres_entries == ["lock:test"]


def test_coordinated_lock_non_critical_acquire_timeout_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_lock = FakeRedisLock(acquire_result=False)
    postgres_entries = _install(monkeypatch, fake_lock)

    with pytest.raises(TimeoutError):
        with redis_lock.coordinated_lock("lock:test", critical=False, wait_seconds=0):
            raise AssertionError("body must not run")

    assert fake_lock.acquire_calls == 1
    assert fake_lock.release_calls == 0
    assert postgres_entries == []


def test_coordinated_lock_release_failure_is_logged_and_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
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
    fake_lock = FakeRedisLock(acquire_result=True)
    _install(monkeypatch, fake_lock)

    @contextmanager
    def forbidden_postgres_advisory_lock(_key: str):
        raise AssertionError("business exceptions must not fallback")
        yield

    monkeypatch.setattr(redis_lock, "postgres_advisory_lock", forbidden_postgres_advisory_lock)

    with pytest.raises(ValueError) as exc_info:
        with redis_lock.coordinated_lock("lock:test"):
            raise ValueError("original business error")

    assert str(exc_info.value) == "original business error"
    assert "generator didn't stop after throw" not in str(exc_info.value)
