"""文件作用：FastAPI 接入/测试 API。

项目关系：本文件依赖 `apps.receiver`、`core.observability`、`core.settings`、`infrastructure.overload` 等 6 个模块；被 `tests.test_stage4_resilience`。
"""



from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from apps.receiver import InboxCommand, receive
from core.observability import log_event, log_process_started
from core.settings import (
    COORDINATION_BACKEND,
    RATE_LIMIT_ASK_PER_MINUTE,
    RATE_LIMIT_INGEST_PER_MINUTE,
    STAGE4_MODE,
    SUIXINJI_ENV,
    TEST_API_DEFAULT_TENANT_ID,
    TEST_API_DEFAULT_USER_ID,
    TEST_API_ENABLED,
    TEST_API_TOKEN,
)
from infrastructure.redis_keys import KEYS
from infrastructure.overload import database_overload_snapshot
from infrastructure.redis_rate_limit import LOCAL_RATE_LIMITER, RedisRateLimiter

app = FastAPI(title="Suixinji Receiver", version="3")


@app.on_event("startup")
def _log_api_startup() -> None:
    """函数功能：`_log_api_startup` 负责记录日志 api startup，服务于本文件职责：FastAPI 接入/测试 API。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    log_process_started("api")


@dataclass(frozen=True)
class TestApiContext:
    """类功能：`TestApiContext` 封装与“FastAPI 接入/测试 API”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    tenant_id: str
    user_id: str | None = None


class ReceiveRequest(BaseModel):
    """类功能：`ReceiveRequest` 封装与“FastAPI 接入/测试 API”相关的数据结构、状态或行为。
    继承关系：继承 `BaseModel`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    message_id: str
    space_id: str
    text: str
    task_type: str = Field(pattern="^(ingest|query|summary)$")
    task_payload: dict = Field(default_factory=dict)
    tenant_id: str = "default"
    user_id: str | None = None


@app.get("/health")
def health() -> dict[str, str]:
    """函数功能：`health` 负责处理 health，服务于本文件职责：FastAPI 接入/测试 API。
    传参：
        无。
    返回结果说明：
        返回 `dict[str, str]`，表示结构化结果、载荷或状态映射。
    """
    return {"status": "ok"}


def _authorize_test_api(
    authorization: str | None,
    tenant_id: str | None,
    user_id: str | None,
) -> TestApiContext:
    """函数功能：`_authorize_test_api` 负责验证 api，服务于本文件职责：FastAPI 接入/测试 API。
    传参：
        authorization: authorization 参数，由调用方传入，类型为 `str | None`。
        tenant_id: 租户标识，用于数据库和 Redis key 的租户隔离，类型为 `str | None`。
        user_id: 用户标识，用于鉴权、限流、会话和数据归属，类型为 `str | None`。
    返回结果说明：
        返回 `TestApiContext` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    authorization = authorization if isinstance(authorization, str) else None
    tenant_id = tenant_id if isinstance(tenant_id, str) else None
    user_id = user_id if isinstance(user_id, str) else None
    if not TEST_API_ENABLED:
        raise HTTPException(status_code=404, detail="not found")
    if SUIXINJI_ENV not in {"dev", "stage4", "test"} and not STAGE4_MODE:
        raise HTTPException(status_code=404, detail="not found")
    if not TEST_API_TOKEN:
        raise HTTPException(status_code=503, detail="test api token is not configured")
    expected = f"Bearer {TEST_API_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"})
    resolved_tenant = (tenant_id or TEST_API_DEFAULT_TENANT_ID or "default").strip()
    resolved_user = (user_id or TEST_API_DEFAULT_USER_ID or "").strip() or None
    if not resolved_tenant:
        raise HTTPException(status_code=400, detail="tenant context is required")
    return TestApiContext(tenant_id=resolved_tenant, user_id=resolved_user)


def _check_rate_limit(request: ReceiveRequest, context: TestApiContext | None = None) -> None:
    """函数功能：`_check_rate_limit` 负责检查 rate limit，服务于本文件职责：FastAPI 接入/测试 API。
    传参：
        request: 请求对象或请求载荷，类型为 `ReceiveRequest`。
        context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息，类型为 `TestApiContext | None`，默认值为 `None`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    if COORDINATION_BACKEND != "redis":
        return
    context = context or TestApiContext(tenant_id=request.tenant_id or "default", user_id=request.user_id)
    action = "ingest" if request.task_type == "ingest" else "ask"
    limit = RATE_LIMIT_INGEST_PER_MINUTE if action == "ingest" else RATE_LIMIT_ASK_PER_MINUTE
    user_id = context.user_id or request.space_id
    try:
        result = RedisRateLimiter().allow(KEYS.rate_user(context.tenant_id, user_id, action), limit)
    except Exception as exc:
        log_event("receiver.rate_limit_unavailable", level="warning", status="degraded", error=type(exc).__name__)
        if request.task_type == "summary":
            raise HTTPException(status_code=503, detail="summary delayed while coordination is unavailable", headers={"Retry-After": "5"})
        overload = database_overload_snapshot()
        if overload.state == "overload" and request.task_type != "ingest":
            log_event(
                "receiver.overload_rejected",
                level="warning",
                status="rejected",
                extra={**overload.to_dict(), "task_type": request.task_type},
            )
            raise HTTPException(status_code=503, detail="service temporarily overloaded", headers={"Retry-After": "2"})
        result = LOCAL_RATE_LIMITER.allow(
            f"local:{user_id}:{action}",
            max(1, limit // 2),
        )
    if result.allowed:
        return
    log_event(
        "receiver.rate_limited",
        level="warning",
        status="rejected",
        space_id=request.space_id,
        message_id=request.message_id,
        extra={"action": action, "retry_after_ms": result.retry_after_ms},
    )
    raise HTTPException(
        status_code=429,
        detail="rate limit exceeded",
        headers={"Retry-After": str(max(1, result.retry_after_ms // 1000))},
    )


@app.post("/v1/commands")
def commands(
    request: ReceiveRequest,
    authorization: str | None = Header(default=None),
    x_suixinji_tenant_id: str | None = Header(default=None),
    x_suixinji_user_id: str | None = Header(default=None),
) -> dict[str, object]:
    """函数功能：`commands` 负责处理 commands，服务于本文件职责：FastAPI 接入/测试 API。
    传参：
        request: 请求对象或请求载荷，类型为 `ReceiveRequest`。
        authorization: authorization 参数，由调用方传入，类型为 `str | None`，默认值为 `Header(default=None)`。
        x_suixinji_tenant_id: x suixinji tenant id 参数，由调用方传入，类型为 `str | None`，默认值为 `Header(default=None)`。
        x_suixinji_user_id: x suixinji user id 参数，由调用方传入，类型为 `str | None`，默认值为 `Header(default=None)`。
    返回结果说明：
        返回 `dict[str, object]`，表示结构化结果、载荷或状态映射。
    """
    context = _authorize_test_api(authorization, x_suixinji_tenant_id, x_suixinji_user_id)
    _check_rate_limit(request, context)
    sender = {"user_id": context.user_id} if context.user_id else {}
    task_payload = dict(request.task_payload)
    if context.user_id:
        task_payload.setdefault("user_id", context.user_id)
    try:
        result = receive(
            InboxCommand(
                source="api",
                message_id=request.message_id,
                tenant_id=context.tenant_id,
                space_id=request.space_id,
                text=request.text,
                task_type=request.task_type,
                task_payload=task_payload,
                sender=sender,
            )
        )
    except SQLAlchemyTimeoutError as exc:
        overload = database_overload_snapshot()
        log_event(
            "receiver.database_pool_timeout",
            level="warning",
            status="rejected",
            space_id=request.space_id,
            message_id=request.message_id,
            error=type(exc).__name__,
            extra={**overload.to_dict(), "task_type": request.task_type},
        )
        raise HTTPException(
            status_code=503,
            detail="service temporarily overloaded",
            headers={"Retry-After": "2"},
        ) from exc
    log_event(
        "receiver.command",
        status="in_progress" if result.in_progress else "duplicate" if result.duplicate else "accepted",
        space_id=request.space_id,
        message_id=request.message_id,
        record_id=result.task_id,
        extra={"task_type": request.task_type, "created": result.created, "duplicate": result.duplicate},
    )
    return {
        "inbox_id": result.inbox_id,
        "task_id": result.task_id,
        "created": result.created,
        "duplicate": result.duplicate,
        "in_progress": result.in_progress,
    }
