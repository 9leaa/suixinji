"""FastAPI test receiver for multi-user and load-test clients."""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel, Field

from apps.receiver import InboxCommand, receive

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


@app.post("/v1/commands")
def commands(request: ReceiveRequest) -> dict[str, object]:
    sender = {"user_id": request.user_id} if request.user_id else {}
    result = receive(
        InboxCommand(
            source="api",
            message_id=request.message_id,
            tenant_id=request.tenant_id,
            space_id=request.space_id,
            text=request.text,
            task_type=request.task_type,
            task_payload=request.task_payload,
            sender=sender,
        )
    )
    return {"inbox_id": result.inbox_id, "task_id": result.task_id, "created": result.created, "duplicate": result.duplicate}
