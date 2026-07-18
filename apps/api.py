"""FastAPI test receiver for multi-user and load-test clients."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from apps.receiver import InboxCommand, receive
from core.observability import log_event
from core.settings import COORDINATION_BACKEND, RATE_LIMIT_ASK_PER_MINUTE, RATE_LIMIT_INGEST_PER_MINUTE
from infrastructure.redis_keys import KEYS
from infrastructure.redis_rate_limit import RedisRateLimiter

app = FastAPI(title="Suixinji Receiver", version="3")


class ReceiveRequest(BaseModel):
    message_id: str
    space_id: str
    text: str
    task_type: str = Field(pattern="^(ingest|query|summary)$")
    task_payload: dict = Field(default_factory=dict)
    tenant_id: str = "default"
    user_id: str | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _check_rate_limit(request: ReceiveRequest) -> None:
    if COORDINATION_BACKEND != "redis":
        return
    action = "ingest" if request.task_type == "ingest" else "ask"
    limit = RATE_LIMIT_INGEST_PER_MINUTE if action == "ingest" else RATE_LIMIT_ASK_PER_MINUTE
    user_id = request.user_id or request.space_id
    try:
        result = RedisRateLimiter().allow(KEYS.rate_user(user_id, action), limit)
    except Exception as exc:
        # Redis is coordination only; PostgreSQL Inbox remains available during a Redis outage.
        log_event("receiver.rate_limit_unavailable", level="warning", status="degraded", error=type(exc).__name__)
        return
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
def commands(request: ReceiveRequest) -> dict[str, object]:
    _check_rate_limit(request)
    sender = {"user_id": request.user_id} if request.user_id else {}
    task_payload = dict(request.task_payload)
    if request.user_id:
        task_payload.setdefault("user_id", request.user_id)
    result = receive(
        InboxCommand(
            source="api",
            message_id=request.message_id,
            tenant_id=request.tenant_id,
            space_id=request.space_id,
            text=request.text,
            task_type=request.task_type,
            task_payload=task_payload,
            sender=sender,
        )
    )
    log_event(
        "receiver.command",
        status="duplicate" if result.duplicate else "accepted",
        space_id=request.space_id,
        message_id=request.message_id,
        record_id=result.task_id,
        extra={"task_type": request.task_type, "created": result.created, "duplicate": result.duplicate},
    )
    return {"inbox_id": result.inbox_id, "task_id": result.task_id, "created": result.created, "duplicate": result.duplicate}
