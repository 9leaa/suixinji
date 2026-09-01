from __future__ import annotations

import pytest

from infrastructure import database


def test_physical_connect_retries_connection_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}
    sleeps: list[float] = []

    def connect(*args: object, **kwargs: object) -> str:
        assert args == ("dsn",)
        assert kwargs == {"connect_timeout": 5}
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise ConnectionError("connection refused")
        return "connected"

    monkeypatch.setattr(database.time, "sleep", sleeps.append)
    monkeypatch.setattr(database, "DATABASE_CONNECT_MAX_ATTEMPTS", 4)
    monkeypatch.setattr(database, "DATABASE_CONNECT_RETRY_BASE_SECONDS", 0.25)
    monkeypatch.setattr(database, "DATABASE_CONNECT_RETRY_MAX_SECONDS", 1.0)

    result = database._connect_with_retry(connect, "dsn", connect_timeout=5)

    assert result == "connected"
    assert attempts["count"] == 3
    assert sleeps == [0.25, 0.5]


def test_physical_connect_stops_after_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}

    def connect() -> None:
        attempts["count"] += 1
        raise ConnectionError("connection refused")

    monkeypatch.setattr(database.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(database, "DATABASE_CONNECT_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(database, "DATABASE_CONNECT_RETRY_BASE_SECONDS", 0.0)

    with pytest.raises(ConnectionError):
        database._connect_with_retry(connect)

    assert attempts["count"] == 3
