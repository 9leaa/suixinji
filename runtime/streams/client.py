"""Small Redis Streams client with groups, ACK, reclaim, and dead letters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from redis import Redis
from redis.exceptions import ResponseError

from core.settings import STREAM_BATCH_SIZE, STREAM_BLOCK_MS, STREAM_CLAIM_IDLE_MS, STREAM_MAXLEN
from infrastructure.redis_client import get_redis
from infrastructure.redis_keys import KEYS, RedisKeys

GROUPS = {
    "ingest": "ingest-workers",
    "query": "query-workers",
    "summary": "summary-workers",
    "memory": "memory-workers",
    "enrichment": "enrichment-workers",
    "delivery": "delivery-workers",
}


@dataclass(frozen=True)
class StreamMessage:
    stream: str
    message_id: str
    fields: dict[str, str]


class StreamClient:
    def __init__(self, client: Redis | None = None, *, keys: RedisKeys = KEYS) -> None:
        self.client = client or get_redis()
        self.keys = keys

    def ensure_group(self, task_type: str) -> tuple[str, str]:
        stream = self.keys.stream(task_type)
        group = GROUPS[task_type]
        try:
            self.client.xgroup_create(stream, group, id="0-0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        return stream, group

    def publish_task(self, event_id: str, payload: dict[str, Any]) -> str:
        task_type = str(payload["task_type"])
        stream = self.keys.stream(task_type)
        fields = {
            "event_id": event_id,
            "task_id": str(payload["task_id"]),
            "task_type": task_type,
            "attempt": str(payload.get("attempt") or 1),
        }
        return str(self.client.xadd(stream, fields, maxlen=max(1000, STREAM_MAXLEN), approximate=True))

    def read(self, task_type: str, consumer: str, *, count: int = STREAM_BATCH_SIZE, block_ms: int = STREAM_BLOCK_MS) -> list[StreamMessage]:
        stream, group = self.ensure_group(task_type)
        response = self.client.xreadgroup(group, consumer, {stream: ">"}, count=max(1, count), block=max(0, block_ms))
        return self._messages(response)

    def reclaim(self, task_type: str, consumer: str, *, min_idle_ms: int = STREAM_CLAIM_IDLE_MS, count: int = STREAM_BATCH_SIZE) -> list[StreamMessage]:
        stream, group = self.ensure_group(task_type)
        response = self.client.xautoclaim(
            stream,
            group,
            consumer,
            min_idle_time=max(1, min_idle_ms),
            start_id="0-0",
            count=max(1, count),
        )
        entries = response[1] if len(response) > 1 else []
        return [StreamMessage(stream, str(message_id), {str(key): str(value) for key, value in fields.items()}) for message_id, fields in entries]

    def ack(self, task_type: str, message_id: str) -> int:
        stream, group = self.ensure_group(task_type)
        return int(self.client.xack(stream, group, message_id))

    def dead_letter(self, message: StreamMessage, *, error: str) -> str:
        fields = {**message.fields, "source_stream": message.stream, "source_message_id": message.message_id, "error": error[:1000]}
        return str(self.client.xadd(self.keys.dead_letter_stream(), fields, maxlen=max(1000, STREAM_MAXLEN), approximate=True))

    @staticmethod
    def _messages(response: Any) -> list[StreamMessage]:
        messages: list[StreamMessage] = []
        for stream, entries in response or []:
            for message_id, fields in entries:
                messages.append(
                    StreamMessage(
                        str(stream),
                        str(message_id),
                        {str(key): str(value) for key, value in fields.items()},
                    )
                )
        return messages
