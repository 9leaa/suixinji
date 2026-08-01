"""文件作用：Redis Streams 客户端。

项目关系：本文件依赖 `core.settings`、`infrastructure.redis_client`、`infrastructure.redis_keys`；被 `runtime.distributed_metrics`、`runtime.streams.__init__`、`runtime.streams.worker`、`scripts.check_distributed_cutover` 等 9 个模块。
"""



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
    """类功能：`StreamMessage` 封装与“Redis Streams 客户端”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    stream: str
    message_id: str
    fields: dict[str, str]


class StreamClient:
    """类功能：`StreamClient` 封装与“Redis Streams 客户端”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(
        self,
        client: Redis | None = None,
        *,
        blocking_client: Redis | None = None,
        keys: RedisKeys = KEYS,
    ) -> None:
        """函数功能：`StreamClient.__init__` 在类 `StreamClient` 中负责初始化实例状态，服务于本文件职责：Redis Streams 客户端。
        传参：
            client: 外部服务或基础设施客户端，类型为 `Redis | None`，默认值为 `None`。
            blocking_client: blocking client 参数，由调用方传入，类型为 `Redis | None`，默认值为 `None`。
            keys: keys 参数，由调用方传入，类型为 `RedisKeys`，默认值为 `KEYS`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.client = client or get_redis()
        self.blocking_client = blocking_client or (client if client is not None else get_blocking_redis())
        self.keys = keys
        self._reclaim_cursors: dict[tuple[str, str], str] = {}
        self._ensured_groups: set[str] = set()

    def ensure_group(self, task_type: str) -> tuple[str, str]:
        """函数功能：`StreamClient.ensure_group` 在类 `StreamClient` 中负责确保 group，服务于本文件职责：Redis Streams 客户端。
        传参：
            task_type: task type 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            返回 `tuple[str, str]`，表示由多个相关值组成的结果。
        """
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
        """函数功能：`StreamClient.publish_task` 在类 `StreamClient` 中负责发布 task，服务于本文件职责：Redis Streams 客户端。
        传参：
            event_id: 事件标识，用于外部事件幂等和审计，类型为 `str`。
            payload: 结构化载荷，通常来自事件、任务或 API 请求，类型为 `dict[str, Any]`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
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
        """函数功能：`StreamClient.read` 在类 `StreamClient` 中负责读取，服务于本文件职责：Redis Streams 客户端。
        传参：
            task_type: task type 参数，由调用方传入，类型为 `str`。
            consumer: consumer 参数，由调用方传入，类型为 `str`。
            count: count 参数，由调用方传入，类型为 `int`，默认值为 `STREAM_BATCH_SIZE`。
            block_ms: block ms 参数，由调用方传入，类型为 `int`，默认值为 `STREAM_BLOCK_MS`。
        返回结果说明：
            返回 `list[StreamMessage]`，表示按条件筛选、构造或查询得到的列表。
        """
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
        """函数功能：`StreamClient.read_many` 在类 `StreamClient` 中负责读取 many，服务于本文件职责：Redis Streams 客户端。
        传参：
            task_types: task types 参数，由调用方传入，类型为 `list[str]`。
            consumer: consumer 参数，由调用方传入，类型为 `str`。
            count: count 参数，由调用方传入，类型为 `int`，默认值为 `1`。
        返回结果说明：
            返回 `list[StreamMessage]`，表示按条件筛选、构造或查询得到的列表。
        """
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
        """函数功能：`StreamClient.reclaim` 在类 `StreamClient` 中负责处理 reclaim，服务于本文件职责：Redis Streams 客户端。
        传参：
            task_type: task type 参数，由调用方传入，类型为 `str`。
            consumer: consumer 参数，由调用方传入，类型为 `str`。
            min_idle_ms: min idle ms 参数，由调用方传入，类型为 `int`，默认值为 `STREAM_CLAIM_IDLE_MS`。
            count: count 参数，由调用方传入，类型为 `int`，默认值为 `STREAM_BATCH_SIZE`。
        返回结果说明：
            返回 `list[StreamMessage]`，表示按条件筛选、构造或查询得到的列表。
        """
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
        """函数功能：`StreamClient.reclaim_cursor` 在类 `StreamClient` 中负责处理 reclaim cursor，服务于本文件职责：Redis Streams 客户端。
        传参：
            task_type: task type 参数，由调用方传入，类型为 `str`。
            consumer: consumer 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        return self._reclaim_cursors.get((task_type, consumer), "0-0")

    def ack(self, task_type: str, message_id: str) -> int:
        """函数功能：`StreamClient.ack` 在类 `StreamClient` 中负责处理 ack，服务于本文件职责：Redis Streams 客户端。
        传参：
            task_type: task type 参数，由调用方传入，类型为 `str`。
            message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str`。
        返回结果说明：
            返回 `int`，表示计算得到的数值结果。
        """
        stream, group = self.ensure_group(task_type)
        return int(self.client.xack(stream, group, message_id))

    def dead_letter(self, message: StreamMessage, *, error: str) -> str:
        """函数功能：`StreamClient.dead_letter` 在类 `StreamClient` 中负责处理 dead letter，服务于本文件职责：Redis Streams 客户端。
        传参：
            message: message 参数，由调用方传入，类型为 `StreamMessage`。
            error: 当前捕获的异常对象，类型为 `str`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        fields = {**message.fields, "source_stream": message.stream, "source_message_id": message.message_id, "error": error[:1000]}
        return str(self.client.xadd(self.keys.dead_letter_stream(), fields, maxlen=max(1000, STREAM_MAXLEN), approximate=True))

    @staticmethod
    def _messages(response: Any) -> list[StreamMessage]:
        """函数功能：`StreamClient._messages` 在类 `StreamClient` 中负责处理 messages，服务于本文件职责：Redis Streams 客户端。
        传参：
            response: 响应对象或响应载荷，类型为 `Any`。
        返回结果说明：
            返回 `list[StreamMessage]`，表示按条件筛选、构造或查询得到的列表。
        """
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
