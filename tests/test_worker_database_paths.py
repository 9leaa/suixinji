from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from core.settings import database_pool_budget
from repositories.postgres import tasks as task_repo
from runtime.streams import worker as worker_module
from runtime.streams.client import StreamMessage
from runtime.streams.worker import HEARTBEAT_SESSION_ROLE, StreamWorker, TaskOutcome


def _claimed_task() -> dict[str, Any]:
    return {
        "id": "task-worker-db",
        "task_type": "ingest",
        "space_id": "space-worker-db",
        "tenant_id": "default",
        "source_message_id": "message-worker-db",
        "lease_token": "lease-worker-db",
        "claim_version": 1,
        "attempt_count": 1,
        "failure_count": 0,
        "defer_count": 0,
        "payload_json": {"inbox_id": "inbox-worker-db"},
    }


class _FakeEvent:
    def __init__(self) -> None:
        self._set = False
        self._wait_calls = 0

    def wait(self, _timeout: float | None = None) -> bool:
        self._wait_calls += 1
        return self._set or self._wait_calls > 1

    def set(self) -> None:
        self._set = True

    def is_set(self) -> bool:
        return self._set


class _ImmediateThread:
    def __init__(self, target, *_args, **_kwargs) -> None:
        self._target = target

    def start(self) -> None:
        self._target()

    def join(self, timeout: float | None = None) -> None:
        del timeout


class _FakeClient:
    def __init__(self) -> None:
        self.acked: list[tuple[str, str]] = []
        self.dead_letters: list[tuple[str, str]] = []

    def ack(self, task_type: str, message_id: str) -> None:
        self.acked.append((task_type, message_id))

    def dead_letter(self, message: StreamMessage, *, error: str) -> None:
        self.dead_letters.append((message.message_id, error))


def test_worker_heartbeat_has_separate_database_budget() -> None:
    assert database_pool_budget(HEARTBEAT_SESSION_ROLE) == (1, 0)


def test_renew_task_lease_uses_requested_session_role(monkeypatch) -> None:
    roles: list[str | None] = []

    class Result:
        def scalar_one_or_none(self) -> str:
            return "task-worker-db"

    class Session:
        def execute(self, *_args, **_kwargs) -> Result:
            return Result()

    @contextmanager
    def fake_session_scope(*, role: str | None = None):
        roles.append(role)
        yield Session()

    monkeypatch.setattr(task_repo, "session_scope", fake_session_scope)

    assert task_repo.renew_task_lease(
        "task-worker-db",
        lease_token="lease-worker-db",
        claim_version=1,
        session_role=HEARTBEAT_SESSION_ROLE,
    )
    assert roles == [HEARTBEAT_SESSION_ROLE]


def test_stream_worker_success_uses_heartbeat_role_and_completes(monkeypatch) -> None:
    heartbeat_roles: list[str | None] = []
    completed: list[str] = []
    client = _FakeClient()

    monkeypatch.setattr(worker_module.threading, "Event", _FakeEvent)
    monkeypatch.setattr(worker_module.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(worker_module, "claim_task", lambda *_args, **_kwargs: _claimed_task())
    monkeypatch.setattr(
        worker_module,
        "renew_task_lease",
        lambda *_args, session_role=None, **_kwargs: heartbeat_roles.append(session_role) or True,
    )
    monkeypatch.setattr(
        worker_module,
        "complete_task",
        lambda task_id, **_kwargs: completed.append(task_id) or True,
    )

    worker = StreamWorker(
        "ingest",
        lambda _task: TaskOutcome(ingest_complete_inbox_id="inbox-worker-db"),
        client=client,
        worker_id="worker-db-success",
    )
    worker._handle(StreamMessage("stream", "1-0", {"task_id": "task-worker-db"}))

    assert heartbeat_roles == [HEARTBEAT_SESSION_ROLE]
    assert completed == ["task-worker-db"]
    assert client.acked == [("ingest", "1-0")]


def test_stream_worker_failure_uses_heartbeat_role_and_fails_task(monkeypatch) -> None:
    heartbeat_roles: list[str | None] = []
    failed: list[tuple[str, str]] = []
    client = _FakeClient()

    monkeypatch.setattr(worker_module.threading, "Event", _FakeEvent)
    monkeypatch.setattr(worker_module.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(worker_module, "claim_task", lambda *_args, **_kwargs: _claimed_task())
    monkeypatch.setattr(
        worker_module,
        "renew_task_lease",
        lambda *_args, session_role=None, **_kwargs: heartbeat_roles.append(session_role) or True,
    )
    monkeypatch.setattr(
        worker_module,
        "fail_task",
        lambda task_id, error, **_kwargs: failed.append((task_id, error)) or "retry",
    )

    def handler(_task: dict[str, Any]) -> None:
        raise RuntimeError("handler exploded")

    worker = StreamWorker("ingest", handler, client=client, worker_id="worker-db-failure")
    worker._handle(StreamMessage("stream", "1-0", {"task_id": "task-worker-db"}))

    assert heartbeat_roles == [HEARTBEAT_SESSION_ROLE]
    assert failed == [("task-worker-db", "RuntimeError: handler exploded")]
    assert client.acked == [("ingest", "1-0")]
