"""文件作用：分布式 handler 的数据库读写路径。

项目关系：本文件依赖 `core.settings`、`repositories.postgres`、`runtime.streams`、`runtime.streams.client` 等 5 个模块；被 暂无静态导入方或仅作为入口脚本执行。
"""


from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from core.settings import database_pool_budget
from repositories.postgres import tasks as task_repo
from runtime.streams import worker as worker_module
from runtime.streams.client import StreamMessage
from runtime.streams.worker import HEARTBEAT_SESSION_ROLE, StreamWorker, TaskOutcome


def _claimed_task() -> dict[str, Any]:
    """函数功能：`_claimed_task` 负责处理 claimed task，服务于本文件职责：分布式 handler 的数据库读写路径。
    传参：
        无。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
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
    """类功能：`_FakeEvent` 封装与“分布式 handler 的数据库读写路径”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(self) -> None:
        """函数功能：`_FakeEvent.__init__` 在类 `_FakeEvent` 中负责初始化实例状态，服务于本文件职责：分布式 handler 的数据库读写路径。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self._set = False
        self._wait_calls = 0

    def wait(self, _timeout: float | None = None) -> bool:
        """函数功能：`_FakeEvent.wait` 在类 `_FakeEvent` 中负责等待，服务于本文件职责：分布式 handler 的数据库读写路径。
        传参：
            _timeout:  timeout 参数，由调用方传入，类型为 `float | None`，默认值为 `None`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        self._wait_calls += 1
        return self._set or self._wait_calls > 1

    def set(self) -> None:
        """函数功能：`_FakeEvent.set` 在类 `_FakeEvent` 中负责设置，服务于本文件职责：分布式 handler 的数据库读写路径。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self._set = True

    def is_set(self) -> bool:
        """函数功能：`_FakeEvent.is_set` 在类 `_FakeEvent` 中负责判断是否为 set，服务于本文件职责：分布式 handler 的数据库读写路径。
        传参：
            无。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        return self._set


class _ImmediateThread:
    """类功能：`_ImmediateThread` 封装与“分布式 handler 的数据库读写路径”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(self, target, *_args, **_kwargs) -> None:
        """函数功能：`_ImmediateThread.__init__` 在类 `_ImmediateThread` 中负责初始化实例状态，服务于本文件职责：分布式 handler 的数据库读写路径。
        传参：
            target: target 参数，由调用方传入。
            *_args:  args 参数，由调用方传入。
            **_kwargs:  kwargs 参数，由调用方传入。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self._target = target

    def start(self) -> None:
        """函数功能：`_ImmediateThread.start` 在类 `_ImmediateThread` 中负责启动，服务于本文件职责：分布式 handler 的数据库读写路径。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self._target()

    def join(self, timeout: float | None = None) -> None:
        """函数功能：`_ImmediateThread.join` 在类 `_ImmediateThread` 中负责拼接，服务于本文件职责：分布式 handler 的数据库读写路径。
        传参：
            timeout: 超时时间，单位由调用方约定，类型为 `float | None`，默认值为 `None`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        del timeout


class _FakeClient:
    """类功能：`_FakeClient` 封装与“分布式 handler 的数据库读写路径”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(self) -> None:
        """函数功能：`_FakeClient.__init__` 在类 `_FakeClient` 中负责初始化实例状态，服务于本文件职责：分布式 handler 的数据库读写路径。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.acked: list[tuple[str, str]] = []
        self.dead_letters: list[tuple[str, str]] = []

    def ack(self, task_type: str, message_id: str) -> None:
        """函数功能：`_FakeClient.ack` 在类 `_FakeClient` 中负责处理 ack，服务于本文件职责：分布式 handler 的数据库读写路径。
        传参：
            task_type: task type 参数，由调用方传入，类型为 `str`。
            message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.acked.append((task_type, message_id))

    def dead_letter(self, message: StreamMessage, *, error: str) -> None:
        """函数功能：`_FakeClient.dead_letter` 在类 `_FakeClient` 中负责处理 dead letter，服务于本文件职责：分布式 handler 的数据库读写路径。
        传参：
            message: message 参数，由调用方传入，类型为 `StreamMessage`。
            error: 当前捕获的异常对象，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.dead_letters.append((message.message_id, error))


def test_worker_heartbeat_has_separate_database_budget() -> None:
    """函数功能：`test_worker_heartbeat_has_separate_database_budget` 负责验证 worker heartbeat has separate database budget 场景，服务于本文件职责：分布式 handler 的数据库读写路径。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    assert database_pool_budget(HEARTBEAT_SESSION_ROLE) == (1, 0)


def test_renew_task_lease_uses_requested_session_role(monkeypatch) -> None:
    """函数功能：`test_renew_task_lease_uses_requested_session_role` 负责验证 renew task lease uses requested session role 场景，服务于本文件职责：分布式 handler 的数据库读写路径。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    roles: list[str | None] = []

    class Result:
        """类功能：`Result` 封装与“分布式 handler 的数据库读写路径”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def scalar_one_or_none(self) -> str:
            """函数功能：`Result.scalar_one_or_none` 在类 `Result` 中负责处理 scalar one or none，服务于本文件职责：分布式 handler 的数据库读写路径。
            传参：
                无。
            返回结果说明：
                返回 `str`，通常是格式化后的文本、标识或路径。
            """
            return "task-worker-db"

    class Session:
        """类功能：`Session` 封装与“分布式 handler 的数据库读写路径”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def execute(self, *_args, **_kwargs) -> Result:
            """函数功能：`Session.execute` 在类 `Session` 中负责执行，服务于本文件职责：分布式 handler 的数据库读写路径。
            传参：
                *_args:  args 参数，由调用方传入。
                **_kwargs:  kwargs 参数，由调用方传入。
            返回结果说明：
                返回 `Result` 类型结果；具体字段和语义由调用方按该对象约定使用。
            """
            return Result()

    @contextmanager
    def fake_session_scope(*, role: str | None = None):
        """函数功能：`fake_session_scope` 负责处理 fake session scope，服务于本文件职责：分布式 handler 的数据库读写路径。
        传参：
            role: role 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        返回结果说明：
            无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
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
    """函数功能：`test_stream_worker_success_uses_heartbeat_role_and_completes` 负责验证 stream worker success uses heartbeat role and completes 场景，服务于本文件职责：分布式 handler 的数据库读写路径。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    heartbeat_roles: list[str | None] = []
    completed: list[str] = []
    events: list[str] = []
    client = _FakeClient()

    monkeypatch.setattr(worker_module.threading, "Event", _FakeEvent)
    monkeypatch.setattr(worker_module.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(worker_module, "log_event", lambda action, **_kwargs: events.append(action))
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
    assert "runtime.stream_message_received" in events
    assert "runtime.task_claimed" in events
    assert "runtime.task_lease_renewed" in events
    assert "runtime.task_completed" in events


def test_stream_worker_failure_uses_heartbeat_role_and_fails_task(monkeypatch) -> None:
    """函数功能：`test_stream_worker_failure_uses_heartbeat_role_and_fails_task` 负责验证 stream worker failure uses heartbeat role and fails task 场景，服务于本文件职责：分布式 handler 的数据库读写路径。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    heartbeat_roles: list[str | None] = []
    failed: list[tuple[str, str]] = []
    events: list[str] = []
    client = _FakeClient()

    monkeypatch.setattr(worker_module.threading, "Event", _FakeEvent)
    monkeypatch.setattr(worker_module.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(worker_module, "log_event", lambda action, **_kwargs: events.append(action))
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
        """函数功能：`handler` 负责处理 handler，服务于本文件职责：分布式 handler 的数据库读写路径。
        传参：
            _task:  task 参数，由调用方传入，类型为 `dict[str, Any]`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        raise RuntimeError("handler exploded")

    worker = StreamWorker("ingest", handler, client=client, worker_id="worker-db-failure")
    worker._handle(StreamMessage("stream", "1-0", {"task_id": "task-worker-db"}))

    assert heartbeat_roles == [HEARTBEAT_SESSION_ROLE]
    assert failed == [("task-worker-db", "RuntimeError: handler exploded")]
    assert client.acked == [("ingest", "1-0")]
    assert "runtime.stream_message_received" in events
    assert "runtime.task_claimed" in events
    assert "runtime.task_lease_renewed" in events
    assert "runtime.task_failed" in events
    assert "runtime.task_retry_scheduled" in events
