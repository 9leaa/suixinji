"""Small Redis Streams client with groups, ACK, reclaim, and dead letters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from redis import Redis
from redis.exceptions import ResponseError

from core.settings import STREAM_BATCH_SIZE, STREAM_BLOCK_MS, STREAM_CLAIM_IDLE_MS, STREAM_MAXLEN
from infrastructure.redis_client import get_blocking_redis, get_redis
from infrastructure.redis_keys import KEYS, RedisKeys

GROUPS = {
    "ingest": "ingest-workers",
    "query": "query-workers",
    "summary": "summary-workers",
    "memory": "memory-workers",
    "memory_embedding": "memory-embedding-workers",
    "enrichment": "enrichment-workers",
    "delivery": "delivery-workers",
}


@dataclass(frozen=True)
class StreamMessage:
    stream: str
    message_id: str
    fields: dict[str, str]


class StreamClient:
    def __init__(
        self,
        client: Redis | None = None,
        *,
        blocking_client: Redis | None = None,
        keys: RedisKeys = KEYS,
    ) -> None:
        self.client = client or get_redis()
        self.blocking_client = blocking_client or (client if client is not None else get_blocking_redis())
        self.keys = keys
        self._reclaim_cursors: dict[tuple[str, str], str] = {}
        self._ensured_groups: set[str] = set()

    def ensure_group(self, task_type: str) -> tuple[str, str]:
        stream = self.keys.stream(task_type)
        group = GROUPS[task_type]
        if task_type in self._ensured_groups:
            return stream, group
        try:
            self.client.xgroup_create(stream, group, id="0-0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._ensured_groups.add(task_type)
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
        try:
            response = self.blocking_client.xreadgroup(
                group,
                consumer,
                {stream: ">"},
                count=max(1, count),
                block=max(0, block_ms),
            )
        except ResponseError as exc:
            if "NOGROUP" not in str(exc):
                raise
            self._ensured_groups.discard(task_type)
            stream, group = self.ensure_group(task_type)
            response = self.blocking_client.xreadgroup(
                group,
                consumer,
                {stream: ">"},
                count=max(1, count),
                block=max(0, block_ms),
            )
        return self._messages(response)

    def read_many(self, task_types: list[str], consumer: str, *, count: int = 1) -> list[StreamMessage]:
        """Poll independent consumer groups in one Redis network round trip."""
        streams_and_groups = [self.ensure_group(task_type) for task_type in task_types]
        pipeline = self.client.pipeline(transaction=False)
        for stream, group in streams_and_groups:
            pipeline.xreadgroup(group, consumer, {stream: ">"}, count=max(1, count))
        messages: list[StreamMessage] = []
        missing_groups: list[str] = []
        for task_type, response in zip(task_types, pipeline.execute(raise_on_error=False), strict=True):
            if isinstance(response, ResponseError):
                if "NOGROUP" not in str(response):
                    raise response
                self._ensured_groups.discard(task_type)
                missing_groups.append(task_type)
                continue
            messages.extend(self._messages(response))
        if missing_groups:
            retry_streams = [self.ensure_group(task_type) for task_type in missing_groups]
            retry_pipeline = self.client.pipeline(transaction=False)
            for stream, group in retry_streams:
                retry_pipeline.xreadgroup(group, consumer, {stream: ">"}, count=max(1, count))
            for response in retry_pipeline.execute():
                messages.extend(self._messages(response))
        return messages

    def reclaim(self, task_type: str, consumer: str, *, min_idle_ms: int = STREAM_CLAIM_IDLE_MS, count: int = STREAM_BATCH_SIZE) -> list[StreamMessage]:
        stream, group = self.ensure_group(task_type)
        cursor_key = (task_type, consumer)
        response = self.client.xautoclaim(
            stream,
            group,
            consumer,
            min_idle_time=max(1, min_idle_ms),
            start_id=self._reclaim_cursors.get(cursor_key, "0-0"),
            count=max(1, count),
        )
        self._reclaim_cursors[cursor_key] = str(response[0] or "0-0")
        entries = response[1] if len(response) > 1 else []
        return [StreamMessage(stream, str(message_id), {str(key): str(value) for key, value in fields.items()}) for message_id, fields in entries]

    def reclaim_cursor(self, task_type: str, consumer: str) -> str:
        return self._reclaim_cursors.get((task_type, consumer), "0-0")

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
