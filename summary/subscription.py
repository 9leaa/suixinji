"""文件作用：自动总结订阅。

项目关系：本文件依赖 `core.settings`、`repositories.postgres`；被 `apps.handlers`、`bot.feishu_bot`、`repositories.postgres.summary`、`summary.reconciliation` 等 6 个模块。
"""



from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.settings import SUMMARY_DEFAULT_TIME

DATA_DIR = Path("data")
SUBSCRIPTIONS_PATH = DATA_DIR / "summary_subscriptions.json"
DEFAULT_SUMMARY_TIME = SUMMARY_DEFAULT_TIME
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_LOCK = threading.RLock()


@dataclass
class SummarySubscription:
    """类功能：`SummarySubscription` 封装与“自动总结订阅”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    space_id: str
    chat_id: str
    enabled: bool = True
    time: str = DEFAULT_SUMMARY_TIME
    range_key: str = "today"
    last_sent_date: str | None = None


def parse_summary_time(value: str) -> str | None:
    """函数功能：`parse_summary_time` 负责解析 summary time，服务于本文件职责：自动总结订阅。
    传参：
        value: 待转换、校验或计算的值，类型为 `str`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
    value = value.strip()
    if not _TIME_RE.match(value):
        return None
    return value


def _load_raw() -> dict[str, dict[str, Any]]:
    """函数功能：`_load_raw` 负责加载 raw，服务于本文件职责：自动总结订阅。
    传参：
        无。
    返回结果说明：
        返回 `dict[str, dict[str, Any]]`，表示结构化结果、载荷或状态映射。
    """
    with _LOCK:
        if not SUBSCRIPTIONS_PATH.exists():
            return {}
        return json.loads(SUBSCRIPTIONS_PATH.read_text(encoding="utf-8"))


def _save_raw(items: dict[str, dict[str, Any]]) -> None:
    """函数功能：`_save_raw` 负责保存 raw，服务于本文件职责：自动总结订阅。
    传参：
        items: 待遍历或处理的元素集合，类型为 `dict[str, dict[str, Any]]`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    with _LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SUBSCRIPTIONS_PATH.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def get_summary_subscription(space_id: str) -> SummarySubscription | None:
    """函数功能：`get_summary_subscription` 负责获取 summary subscription，服务于本文件职责：自动总结订阅。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `SummarySubscription | None`；未命中或无需处理时可返回 `None`。
    """
    raw = _load_raw().get(space_id)
    if raw is None:
        return None
    return SummarySubscription(**raw)


def list_enabled_summary_subscriptions() -> list[SummarySubscription]:
    """函数功能：`list_enabled_summary_subscriptions` 负责列出 enabled summary subscriptions，服务于本文件职责：自动总结订阅。
    传参：
        无。
    返回结果说明：
        返回 `list[SummarySubscription]`，表示按条件筛选、构造或查询得到的列表。
    """
    return [
        SummarySubscription(**item)
        for item in _load_raw().values()
        if item.get("enabled")
    ]


def enable_summary_subscription(space_id: str, chat_id: str) -> SummarySubscription:
    """函数功能：`enable_summary_subscription` 负责处理 enable summary subscription，服务于本文件职责：自动总结订阅。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        chat_id: chat id 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `SummarySubscription` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    items = _load_raw()
    old = items.get(space_id, {})
    sub = SummarySubscription(
        space_id=space_id,
        chat_id=chat_id,
        enabled=True,
        time=str(old.get("time") or DEFAULT_SUMMARY_TIME),
        range_key=str(old.get("range_key") or "today"),
        last_sent_date=old.get("last_sent_date"),
    )
    items[space_id] = asdict(sub)
    _save_raw(items)
    return sub


def disable_summary_subscription(space_id: str) -> SummarySubscription | None:
    """函数功能：`disable_summary_subscription` 负责处理 disable summary subscription，服务于本文件职责：自动总结订阅。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `SummarySubscription | None`；未命中或无需处理时可返回 `None`。
    """
    items = _load_raw()
    old = items.get(space_id)
    if old is None:
        return None
    old["enabled"] = False
    items[space_id] = old
    _save_raw(items)
    return SummarySubscription(**old)


def update_summary_time(space_id: str, chat_id: str, time_value: str) -> SummarySubscription:
    """函数功能：`update_summary_time` 负责更新 summary time，服务于本文件职责：自动总结订阅。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        chat_id: chat id 参数，由调用方传入，类型为 `str`。
        time_value: time value 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `SummarySubscription` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    parsed = parse_summary_time(time_value)
    if parsed is None:
        raise ValueError("time must be HH:MM, for example 22:00")

    items = _load_raw()
    old = items.get(space_id, {})
    sub = SummarySubscription(
        space_id=space_id,
        chat_id=chat_id,
        enabled=bool(old.get("enabled", True)),
        time=parsed,
        range_key=str(old.get("range_key") or "today"),
        last_sent_date=old.get("last_sent_date"),
    )
    items[space_id] = asdict(sub)
    _save_raw(items)
    return sub


def mark_summary_sent(space_id: str, sent_date: str) -> None:
    """函数功能：`mark_summary_sent` 负责标记 summary sent，服务于本文件职责：自动总结订阅。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        sent_date: sent date 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    items = _load_raw()
    old = items.get(space_id)
    if old is None:
        return
    old["last_sent_date"] = sent_date
    items[space_id] = old
    _save_raw(items)


from core.settings import STORAGE_BACKEND as _STORAGE_BACKEND

if _STORAGE_BACKEND == "postgres":
    from repositories.postgres import summary as _postgres_summary

    get_summary_subscription = _postgres_summary.get_summary_subscription
    list_enabled_summary_subscriptions = _postgres_summary.list_enabled_summary_subscriptions
    enable_summary_subscription = _postgres_summary.enable_summary_subscription
    disable_summary_subscription = _postgres_summary.disable_summary_subscription
    update_summary_time = _postgres_summary.update_summary_time
    mark_summary_sent = _postgres_summary.mark_summary_sent
