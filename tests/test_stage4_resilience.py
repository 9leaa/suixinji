"""文件作用：Stage 4 失效/恢复路径。

项目关系：本文件依赖 `apps`、`apps.api`、`infrastructure.database`、`infrastructure.schema` 等 8 个模块；被 暂无静态导入方或仅作为入口脚本执行。
"""


from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from apps import api, handlers, receiver
from apps.api import ReceiveRequest
from infrastructure.database import session_scope
from infrastructure.schema import InboxMessage, Memory, Space, Task, Tenant
from memory.models import MemoryCandidate
from repositories.postgres import memory as postgres_memory
from repositories.postgres.common import ensure_tenant_space
from repositories.postgres.dispatch import DispatchResult


def _api_request(task_type: str = "ingest") -> ReceiveRequest:
    """函数功能：`_api_request` 负责处理 api request，服务于本文件职责：Stage 4 失效/恢复路径。
    传参：
        task_type: task type 参数，由调用方传入，类型为 `str`，默认值为 `'ingest'`。
    返回结果说明：
        返回 `ReceiveRequest` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return ReceiveRequest(
        message_id="stage4-message",
        space_id="stage4-space",
        text="hello",
        task_type=task_type,
        user_id="stage4-user",
    )


def test_api_keeps_accepting_when_redis_rate_limit_is_unavailable(monkeypatch):
    """函数功能：`test_api_keeps_accepting_when_redis_rate_limit_is_unavailable` 负责验证 api keeps accepting when redis rate limit is unavailable 场景，服务于本文件职责：Stage 4 失效/恢复路径。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    class BrokenLimiter:
        """类功能：`BrokenLimiter` 封装与“Stage 4 失效/恢复路径”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def allow(self, *_args, **_kwargs):
            """函数功能：`BrokenLimiter.allow` 在类 `BrokenLimiter` 中负责处理 allow，服务于本文件职责：Stage 4 失效/恢复路径。
            传参：
                *_args:  args 参数，由调用方传入。
                **_kwargs:  kwargs 参数，由调用方传入。
            返回结果说明：
                无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
            """
            raise ConnectionError("redis unavailable")

    monkeypatch.setattr(api, "COORDINATION_BACKEND", "redis")
    monkeypatch.setattr(api, "RedisRateLimiter", BrokenLimiter)
    monkeypatch.setattr(api, "database_overload_snapshot", lambda: SimpleNamespace(state="normal", to_dict=lambda: {}))
    api._check_rate_limit(_api_request())


def test_api_delays_summary_when_redis_rate_limit_is_unavailable(monkeypatch):
    """函数功能：`test_api_delays_summary_when_redis_rate_limit_is_unavailable` 负责验证 api delays summary when redis rate limit is unavailable 场景，服务于本文件职责：Stage 4 失效/恢复路径。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    class BrokenLimiter:
        """类功能：`BrokenLimiter` 封装与“Stage 4 失效/恢复路径”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def allow(self, *_args, **_kwargs):
            """函数功能：`BrokenLimiter.allow` 在类 `BrokenLimiter` 中负责处理 allow，服务于本文件职责：Stage 4 失效/恢复路径。
            传参：
                *_args:  args 参数，由调用方传入。
                **_kwargs:  kwargs 参数，由调用方传入。
            返回结果说明：
                无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
            """
            raise ConnectionError("redis unavailable")

    monkeypatch.setattr(api, "COORDINATION_BACKEND", "redis")
    monkeypatch.setattr(api, "RedisRateLimiter", BrokenLimiter)
    monkeypatch.setattr(api, "database_overload_snapshot", lambda: SimpleNamespace(state="normal", to_dict=lambda: {}))
    with pytest.raises(HTTPException) as exc_info:
        api._check_rate_limit(_api_request("summary"))
    assert exc_info.value.status_code == 503


def test_api_returns_429_for_measured_rate_limit(monkeypatch):
    """函数功能：`test_api_returns_429_for_measured_rate_limit` 负责验证 api returns 429 for measured rate limit 场景，服务于本文件职责：Stage 4 失效/恢复路径。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    class RejectingLimiter:
        """类功能：`RejectingLimiter` 封装与“Stage 4 失效/恢复路径”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def allow(self, *_args, **_kwargs):
            """函数功能：`RejectingLimiter.allow` 在类 `RejectingLimiter` 中负责处理 allow，服务于本文件职责：Stage 4 失效/恢复路径。
            传参：
                *_args:  args 参数，由调用方传入。
                **_kwargs:  kwargs 参数，由调用方传入。
            返回结果说明：
                返回计算后的结果对象；具体类型取决于实际执行分支。
            """
            return SimpleNamespace(allowed=False, retry_after_ms=1500)

    monkeypatch.setattr(api, "COORDINATION_BACKEND", "redis")
    monkeypatch.setattr(api, "RedisRateLimiter", RejectingLimiter)
    monkeypatch.setattr(api, "database_overload_snapshot", lambda: SimpleNamespace(state="normal", to_dict=lambda: {}))
    with pytest.raises(HTTPException) as exc_info:
        api._check_rate_limit(_api_request())
    assert exc_info.value.status_code == 429


def test_api_ignores_local_database_pressure_when_redis_rate_limit_is_available(monkeypatch):
    """函数功能：`test_api_ignores_local_database_pressure_when_redis_rate_limit_is_available` 负责验证 api ignores local database pressure when redis rate limit is available 场景，服务于本文件职责：Stage 4 失效/恢复路径。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    class AllowingLimiter:
        """类功能：`AllowingLimiter` 封装与“Stage 4 失效/恢复路径”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def allow(self, *_args, **_kwargs):
            """函数功能：`AllowingLimiter.allow` 在类 `AllowingLimiter` 中负责处理 allow，服务于本文件职责：Stage 4 失效/恢复路径。
            传参：
                *_args:  args 参数，由调用方传入。
                **_kwargs:  kwargs 参数，由调用方传入。
            返回结果说明：
                返回计算后的结果对象；具体类型取决于实际执行分支。
            """
            return SimpleNamespace(allowed=True, retry_after_ms=0)

    monkeypatch.setattr(api, "COORDINATION_BACKEND", "redis")
    monkeypatch.setattr(api, "RedisRateLimiter", AllowingLimiter)
    monkeypatch.setattr(
        api,
        "database_overload_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("DB backpressure is only a Redis-outage fallback")),
    )
    api._check_rate_limit(_api_request("query"))


def test_api_rejects_query_when_redis_and_database_are_unavailable(monkeypatch):
    """函数功能：`test_api_rejects_query_when_redis_and_database_are_unavailable` 负责验证 api rejects query when redis and database are unavailable 场景，服务于本文件职责：Stage 4 失效/恢复路径。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    class BrokenLimiter:
        """类功能：`BrokenLimiter` 封装与“Stage 4 失效/恢复路径”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def allow(self, *_args, **_kwargs):
            """函数功能：`BrokenLimiter.allow` 在类 `BrokenLimiter` 中负责处理 allow，服务于本文件职责：Stage 4 失效/恢复路径。
            传参：
                *_args:  args 参数，由调用方传入。
                **_kwargs:  kwargs 参数，由调用方传入。
            返回结果说明：
                无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
            """
            raise ConnectionError("redis unavailable")

    monkeypatch.setattr(api, "COORDINATION_BACKEND", "redis")
    monkeypatch.setattr(api, "RedisRateLimiter", BrokenLimiter)
    monkeypatch.setattr(api, "database_overload_snapshot", lambda: SimpleNamespace(state="overload", to_dict=lambda: {}))
    with pytest.raises(HTTPException) as exc_info:
        api._check_rate_limit(_api_request("query"))
    assert exc_info.value.status_code == 503


def test_api_returns_retryable_503_when_receiver_pool_times_out(monkeypatch):
    """函数功能：`test_api_returns_retryable_503_when_receiver_pool_times_out` 负责验证 api returns retryable 503 when receiver pool times out 场景，服务于本文件职责：Stage 4 失效/恢复路径。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(api, "TEST_API_ENABLED", True)
    monkeypatch.setattr(api, "TEST_API_TOKEN", "stage4-token")
    monkeypatch.setattr(api, "SUIXINJI_ENV", "stage4")
    monkeypatch.setattr(api, "_check_rate_limit", lambda _request, _context=None: None)
    monkeypatch.setattr(api, "receive", lambda _command: (_ for _ in ()).throw(SQLAlchemyTimeoutError()))
    monkeypatch.setattr(api, "database_overload_snapshot", lambda: SimpleNamespace(state="overload", to_dict=lambda: {}))
    with pytest.raises(HTTPException) as exc_info:
        api.commands(_api_request(), authorization="Bearer stage4-token")
    assert exc_info.value.status_code == 503
    assert exc_info.value.headers == {"Retry-After": "2"}


def test_test_api_rejects_disabled_and_unauthenticated_access(monkeypatch):
    """函数功能：`test_test_api_rejects_disabled_and_unauthenticated_access` 负责验证 test api rejects disabled and unauthenticated access 场景，服务于本文件职责：Stage 4 失效/恢复路径。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(api, "TEST_API_ENABLED", False)
    with pytest.raises(HTTPException) as exc_info:
        api.commands(_api_request())
    assert exc_info.value.status_code == 404

    monkeypatch.setattr(api, "TEST_API_ENABLED", True)
    monkeypatch.setattr(api, "TEST_API_TOKEN", "stage4-token")
    monkeypatch.setattr(api, "SUIXINJI_ENV", "stage4")
    with pytest.raises(HTTPException) as exc_info:
        api.commands(_api_request())
    assert exc_info.value.status_code == 401


def test_test_api_ignores_body_tenant_and_uses_auth_context(monkeypatch):
    """函数功能：`test_test_api_ignores_body_tenant_and_uses_auth_context` 负责验证 test api ignores body tenant and uses auth context 场景，服务于本文件职责：Stage 4 失效/恢复路径。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    captured = {}
    monkeypatch.setattr(api, "TEST_API_ENABLED", True)
    monkeypatch.setattr(api, "TEST_API_TOKEN", "stage4-token")
    monkeypatch.setattr(api, "SUIXINJI_ENV", "stage4")
    monkeypatch.setattr(api, "_check_rate_limit", lambda _request, _context=None: None)

    def receive_command(command):
        """函数功能：`receive_command` 负责接收 command，服务于本文件职责：Stage 4 失效/恢复路径。
        传参：
            command: command 参数，由调用方传入。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
        captured["tenant_id"] = command.tenant_id
        captured["sender"] = command.sender
        return DispatchResult("inbox-1", "task-1", True, False)

    request = _api_request()
    request.tenant_id = "body-tenant"
    monkeypatch.setattr(api, "receive", receive_command)
    api.commands(
        request,
        authorization="Bearer stage4-token",
        x_suixinji_tenant_id="auth-tenant",
        x_suixinji_user_id="auth-user",
    )
    assert captured["tenant_id"] == "auth-tenant"
    assert captured["sender"] == {"user_id": "auth-user"}


def test_receiver_falls_back_to_postgres_when_redis_idempotency_is_down(monkeypatch):
    """函数功能：`test_receiver_falls_back_to_postgres_when_redis_idempotency_is_down` 负责验证 receiver falls back to postgres when redis idempotency is down 场景，服务于本文件职责：Stage 4 失效/恢复路径。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    class BrokenIdempotency:
        """类功能：`BrokenIdempotency` 封装与“Stage 4 失效/恢复路径”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def __init__(self):
            """函数功能：`BrokenIdempotency.__init__` 在类 `BrokenIdempotency` 中负责初始化实例状态，服务于本文件职责：Stage 4 失效/恢复路径。
            传参：
                无。
            返回结果说明：
                无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
            """
            raise ConnectionError("redis unavailable")

    expected = DispatchResult("inbox-1", "task-1", True, False)
    monkeypatch.setattr(receiver, "COORDINATION_BACKEND", "redis")
    monkeypatch.setattr(receiver, "IdempotencyStore", BrokenIdempotency)
    monkeypatch.setattr(receiver, "receive_command", lambda **_kwargs: expected)
    result = receiver.receive(
        receiver.InboxCommand(
            source="stage4",
            message_id="message-1",
            space_id="space-1",
            text="hello",
            task_type="ingest",
            task_payload={},
        )
    )
    assert result == expected


def test_receiver_returns_in_progress_without_postgres_for_processing_idempotency(monkeypatch):
    """函数功能：`test_receiver_returns_in_progress_without_postgres_for_processing_idempotency` 负责验证 receiver returns in progress without postgres for processing idempotency 场景，服务于本文件职责：Stage 4 失效/恢复路径。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    class ProcessingIdempotency:
        """类功能：`ProcessingIdempotency` 封装与“Stage 4 失效/恢复路径”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def get(self, _key):
            """函数功能：`ProcessingIdempotency.get` 在类 `ProcessingIdempotency` 中负责获取，服务于本文件职责：Stage 4 失效/恢复路径。
            传参：
                _key:  key 参数，由调用方传入。
            返回结果说明：
                返回计算后的结果对象；具体类型取决于实际执行分支。
            """
            return "processing"

    monkeypatch.setattr(receiver, "COORDINATION_BACKEND", "redis")
    monkeypatch.setattr(receiver, "IdempotencyStore", ProcessingIdempotency)
    monkeypatch.setattr(
        receiver,
        "receive_command",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not access PostgreSQL")),
    )
    result = receiver.receive(
        receiver.InboxCommand(
            source="stage4",
            message_id="message-processing",
            space_id="space-1",
            text="hello",
            task_type="ingest",
            task_payload={},
        )
    )
    assert result.in_progress is True
    assert result.duplicate is False


def test_fake_delivery_never_calls_external_sender(monkeypatch):
    """函数功能：`test_fake_delivery_never_calls_external_sender` 负责验证 fake delivery never calls external sender 场景，服务于本文件职责：Stage 4 失效/恢复路径。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    sent = []
    reservation_kwargs = {}

    def reserve(*_args, **kwargs):
        """函数功能：`reserve` 负责预约，服务于本文件职责：Stage 4 失效/恢复路径。
        传参：
            *_args:  args 参数，由调用方传入。
            **kwargs: kwargs 参数，由调用方传入。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
        reservation_kwargs.update(kwargs)
        return SimpleNamespace(status="reserved")

    monkeypatch.setattr(handlers, "FAKE_EXTERNALS", True)
    monkeypatch.setattr(handlers, "reserve_delivery", reserve)
    monkeypatch.setattr(handlers, "mark_sent", lambda key: sent.append(key))
    monkeypatch.setattr(handlers, "send_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("external send")))
    handlers.handle_delivery(
        {
            "id": "task-1",
            "tenant_id": "tenant-1",
            "space_id": "space-1",
            "source_message_id": "message-1",
            "payload_json": {
                "delivery_key": "delivery-1",
                "delivery_type": "load",
                "chat_id": "fake-chat",
                "text": "fake text",
            },
        }
    )
    assert sent == ["delivery-1"]
    assert reservation_kwargs["tenant_id"] == "tenant-1"


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="PostgreSQL integration URL is not configured")
def test_memory_insert_inherits_space_tenant():
    """函数功能：`test_memory_insert_inherits_space_tenant` 负责验证 memory insert inherits space tenant 场景，服务于本文件职责：Stage 4 失效/恢复路径。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    suffix = uuid.uuid4().hex
    tenant_id = f"stage4-tenant-{suffix}"
    space_id = f"stage4-space-{suffix}"
    try:
        with session_scope() as session:
            ensure_tenant_space(session, space_id, tenant_id=tenant_id, source="stage4")
        created = postgres_memory.insert_memory(
            space_id,
            MemoryCandidate("preference", "User likes deterministic tests", 0.8, 0.9),
            source_note_id=f"note-{suffix}",
        )
        with session_scope() as session:
            assert session.get(Memory, created.id).tenant_id == tenant_id
    finally:
        with session_scope() as session:
            session.execute(delete(Tenant).where(Tenant.id == tenant_id))


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="PostgreSQL integration URL is not configured")
def test_concurrent_space_creation_handles_all_unique_constraints():
    """函数功能：`test_concurrent_space_creation_handles_all_unique_constraints` 负责验证 concurrent space creation handles all unique constraints 场景，服务于本文件职责：Stage 4 失效/恢复路径。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    suffix = uuid.uuid4().hex
    tenant_id = f"stage4-tenant-{suffix}"
    space_id = f"stage4-space-{suffix}"

    def create_space(_index: int) -> None:
        """函数功能：`create_space` 负责创建 space，服务于本文件职责：Stage 4 失效/恢复路径。
        传参：
            _index:  index 参数，由调用方传入，类型为 `int`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        with session_scope() as session:
            ensure_tenant_space(session, space_id, tenant_id=tenant_id, source="stage4")

    try:
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(create_space, range(24)))
    finally:
        with session_scope() as session:
            session.execute(delete(Tenant).where(Tenant.id == tenant_id))


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="PostgreSQL integration URL is not configured")
def test_same_source_space_and_message_are_isolated_by_tenant():
    """函数功能：`test_same_source_space_and_message_are_isolated_by_tenant` 负责验证 same source space and message are isolated by tenant 场景，服务于本文件职责：Stage 4 失效/恢复路径。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    suffix = uuid.uuid4().hex
    tenant_a = f"stage4-tenant-a-{suffix}"
    tenant_b = f"stage4-tenant-b-{suffix}"
    source_space_id = f"same-source-space-{suffix}"
    message_id = f"same-message-{suffix}"
    try:
        result_a = receiver.receive(
            receiver.InboxCommand(
                source="stage4-api",
                tenant_id=tenant_a,
                message_id=message_id,
                space_id=source_space_id,
                text="tenant a",
                task_type="ingest",
                task_payload={},
            )
        )
        result_b = receiver.receive(
            receiver.InboxCommand(
                source="stage4-api",
                tenant_id=tenant_b,
                message_id=message_id,
                space_id=source_space_id,
                text="tenant b",
                task_type="ingest",
                task_payload={},
            )
        )
        assert result_a.created is True
        assert result_b.created is True
        with session_scope() as session:
            inbox_rows = list(
                session.execute(
                    select(InboxMessage.tenant_id, InboxMessage.space_id)
                    .where(InboxMessage.source == "stage4-api", InboxMessage.source_message_id == message_id)
                    .order_by(InboxMessage.tenant_id)
                )
            )
            assert [row[0] for row in inbox_rows] == [tenant_a, tenant_b]
            assert len({row[1] for row in inbox_rows}) == 2
            spaces = list(
                session.execute(
                    select(Space.tenant_id, Space.source_space_id)
                    .where(Space.tenant_id.in_([tenant_a, tenant_b]))
                    .order_by(Space.tenant_id)
                )
            )
            assert spaces == [(tenant_a, source_space_id), (tenant_b, source_space_id)]
    finally:
        with session_scope() as session:
            session.execute(delete(Tenant).where(Tenant.id.in_([tenant_a, tenant_b])))
        with session_scope() as session:
            assert session.execute(select(InboxMessage.id).where(InboxMessage.tenant_id.in_([tenant_a, tenant_b]))).first() is None
            assert session.execute(select(Task.id).where(Task.tenant_id.in_([tenant_a, tenant_b]))).first() is None
            assert session.execute(select(Space.id).where(Space.tenant_id.in_([tenant_a, tenant_b]))).first() is None
