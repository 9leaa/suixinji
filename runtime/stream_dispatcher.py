"""文件作用：Stream 发布适配。

项目关系：本文件依赖 `core.settings`、`repositories.postgres.dispatch`、`runtime.delivery_store`、`runtime.task`；被 `apps.scheduler`。
"""



from __future__ import annotations

from collections.abc import Callable

from core.settings import WORKER_MAX_ATTEMPTS
from repositories.postgres.dispatch import enqueue_task
from runtime.delivery_store import manual_summary_key, query_key
from runtime.task import Task


class StreamTaskDispatcher:
    """类功能：`StreamTaskDispatcher` 封装与“Stream 发布适配”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def submit_query(self, space_id: str, question: str, chat_id: str, message_id: str | None = None) -> Task:
        """函数功能：`StreamTaskDispatcher.submit_query` 在类 `StreamTaskDispatcher` 中负责查询 submit，服务于本文件职责：Stream 发布适配。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            question: 用户问题文本，类型为 `str`。
            chat_id: chat id 参数，由调用方传入，类型为 `str`。
            message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str | None`，默认值为 `None`。
        返回结果说明：
            返回 `Task` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        message_key = message_id or "unknown"
        payload = {
            "question": question,
            "chat_id": chat_id,
            "delivery_key": query_key(space_id, message_key),
            "delivery_type": "query",
        }
        task_id, _ = enqueue_task(
            task_type="query",
            space_id=space_id,
            source_message_id=message_id,
            idempotency_key=f"query:{space_id}:{message_key}",
            payload=payload,
            max_attempts=WORKER_MAX_ATTEMPTS,
        )
        return Task(task_id, "query", space_id, message_id, payload)

    def submit_summary(
        self,
        space_id: str,
        range_key: str,
        chat_id: str,
        message_id: str | None = None,
        on_success: Callable[[], None] | None = None,
        delivery_key: str | None = None,
        delivery_type: str | None = None,
    ) -> Task:
        """函数功能：`StreamTaskDispatcher.submit_summary` 在类 `StreamTaskDispatcher` 中负责处理 submit summary，服务于本文件职责：Stream 发布适配。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            range_key: range key 参数，由调用方传入，类型为 `str`。
            chat_id: chat id 参数，由调用方传入，类型为 `str`。
            message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str | None`，默认值为 `None`。
            on_success: on success 参数，由调用方传入，类型为 `Callable[[], None] | None`，默认值为 `None`。
            delivery_key: delivery key 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
            delivery_type: delivery type 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        返回结果说明：
            返回 `Task` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        del on_success
        key = delivery_key or manual_summary_key(space_id, message_id or "scheduled")
        payload = {
            "range_key": range_key,
            "chat_id": chat_id,
            "delivery_key": key,
            "delivery_type": delivery_type or "manual_summary",
        }
        if payload["delivery_type"] == "auto_summary":
            payload["sent_date"] = key.rsplit(":", 1)[-1]
        task_id, _ = enqueue_task(
            task_type="summary",
            space_id=space_id,
            source_message_id=message_id,
            idempotency_key=f"summary:{key}",
            payload=payload,
            max_attempts=WORKER_MAX_ATTEMPTS,
        )
        return Task(task_id, "summary", space_id, message_id, payload)
