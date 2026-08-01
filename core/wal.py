"""文件作用：本地写前日志。

项目关系：本文件依赖 `core.file_lock`、`core.settings`、`repositories.postgres`；被 `P1_test`、`bot.feishu_bot`、`core.worker`、`main` 等 7 个模块。
"""


from __future__ import annotations

import json
#生成唯一id
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from core.file_lock import locked_space, safe_space_id


DATA_DIR = Path("data")
CACHE_DIR = DATA_DIR / "cache"


@dataclass
class WalRecord:
    """类功能：`WalRecord` 封装与“本地写前日志”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """

    id: str
    source: str
    event_id: str | None
    message_id: str
    space_id: str
    chat_id: str | None
    chat_type: str
    sender: dict[str, Any]
    ts: str
    text: str
    status: str = "pending"
    sensitivity: str = "normal"
    # pending 表示数据刚进 WAL，还没被 worker 处理。


def wal_path(space_id: str) -> Path:
    """函数功能：`wal_path` 负责处理 wal path，服务于本文件职责：本地写前日志。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `Path` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    #如果父目录data不存在，也一起创建
    #如果存在则不报错
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{safe_space_id(space_id)}.jsonl"


def list_wal_space_ids() -> list[str]:
    """函数功能：`list_wal_space_ids` 负责列出 wal space ids，服务于本文件职责：本地写前日志。
    传参：
        无。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
    if not CACHE_DIR.exists():
        return []

    return sorted(path.stem for path in CACHE_DIR.glob("*.jsonl"))


def append_record(record: WalRecord) -> None:
    """函数功能：`append_record` 负责追加 record，服务于本文件职责：本地写前日志。
    传参：
        record: 待处理或持久化的记录对象，类型为 `WalRecord`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    path = wal_path(record.space_id)

    #参数a表示追加、asdict是将dataclass转成普通dict
    #然后dumps吧dict转为JSON字符串
    with locked_space(record.space_id):
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def load_records(space_id: str) -> list[dict[str, Any]]:
    """函数功能：`load_records` 负责加载 records，服务于本文件职责：本地写前日志。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    path = wal_path(space_id)
    with locked_space(space_id):
        if not path.exists():
            return []

        records = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))

        return records


def message_exists(space_id: str, message_id: str) -> bool:
    """函数功能：`message_exists` 负责处理 message exists，服务于本文件职责：本地写前日志。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    #只要有一条记录满足条件，就返回 True；全部都不满足才返回 False。
    return any(
        record.get("message_id") == message_id
        for record in load_records(space_id)
    )


# *表示后面的参数必须是用关键字传参
def create_pending_record(
    *,
    message_id: str,
    space_id: str,
    text: str,
    event_id: str | None = None,
    chat_id: str | None = None,
    chat_type: str = "p2p",
    sender: dict[str, Any] | None = None,
) -> WalRecord:
    """函数功能：`create_pending_record` 负责创建 pending record，服务于本文件职责：本地写前日志。
    传参：
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        text: 输入文本内容，类型为 `str`。
        event_id: 事件标识，用于外部事件幂等和审计，类型为 `str | None`，默认值为 `None`。
        chat_id: chat id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        chat_type: chat type 参数，由调用方传入，类型为 `str`，默认值为 `'p2p'`。
        sender: sender 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
    返回结果说明：
        返回 `WalRecord` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return WalRecord(
        id=str(uuid.uuid4()),
        source="feishu",
        event_id=event_id,
        message_id=message_id,
        space_id=space_id,
        chat_id=chat_id,
        chat_type=chat_type,
        sender=sender or {},
        ts=datetime.now().astimezone().isoformat(),
        text=text,
        status="pending",
    )


def create_blocked_sensitive_record(
    *,
    message_id: str,
    space_id: str,
    category: str,
    event_id: str | None = None,
    chat_id: str | None = None,
    chat_type: str = "p2p",
    sender: dict[str, Any] | None = None,
) -> WalRecord:
    """函数功能：`create_blocked_sensitive_record` 负责创建 blocked sensitive record，服务于本文件职责：本地写前日志。
    传参：
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        category: category 参数，由调用方传入，类型为 `str`。
        event_id: 事件标识，用于外部事件幂等和审计，类型为 `str | None`，默认值为 `None`。
        chat_id: chat id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        chat_type: chat type 参数，由调用方传入，类型为 `str`，默认值为 `'p2p'`。
        sender: sender 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
    返回结果说明：
        返回 `WalRecord` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return WalRecord(
        id=str(uuid.uuid4()),
        source="feishu",
        event_id=event_id,
        message_id=message_id,
        space_id=space_id,
        chat_id=chat_id,
        chat_type=chat_type,
        sender=sender or {},
        ts=datetime.now().astimezone().isoformat(),
        text="[敏感内容已拦截，原文未保存]",
        status="blocked_sensitive",
        sensitivity=category or "sensitive",
    )


def append_message_once(record: WalRecord) -> bool:
    """函数功能：`append_message_once` 负责追加 message once，服务于本文件职责：本地写前日志。
    传参：
        record: 待处理或持久化的记录对象，类型为 `WalRecord`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    with locked_space(record.space_id):
        if message_exists(record.space_id, record.message_id):
            return False

        append_record(record)
        return True


def load_pending_records(space_id: str) -> list[dict[str, Any]]:
    """函数功能：`load_pending_records` 负责加载 pending records，服务于本文件职责：本地写前日志。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    return [
        record
        for record in load_records(space_id)
        if record.get("status") == "pending"
    ]


def mark_processed(space_id: str, record_id: str) -> None:
    """函数功能：`mark_processed` 负责标记 processed，服务于本文件职责：本地写前日志。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        record_id: record id 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    path = wal_path(space_id)
    with locked_space(space_id):
        records = load_records(space_id)

        for record in records:
            if record.get("id") == record_id:
                record["status"] = "processed"

        with path.open("w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")


def mark_sensitive_blocked(
    space_id: str,
    record_id: str,
    category: str = "sensitive",
    *,
    preserve_pending: bool = False,
) -> None:
    """函数功能：`mark_sensitive_blocked` 负责标记 sensitive blocked，服务于本文件职责：本地写前日志。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        record_id: record id 参数，由调用方传入，类型为 `str`。
        category: category 参数，由调用方传入，类型为 `str`，默认值为 `'sensitive'`。
        preserve_pending: preserve pending 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    path = wal_path(space_id)
    with locked_space(space_id):
        records = load_records(space_id)
        for record in records:
            if record.get("id") != record_id:
                continue
            record["text"] = "[敏感内容已拦截，原文未保存]"
            record["status"] = "pending" if preserve_pending else "blocked_sensitive"
            record["sensitivity"] = category or "sensitive"
        with path.open("w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")


from core.settings import STORAGE_BACKEND as _STORAGE_BACKEND

if _STORAGE_BACKEND == "postgres":
    from repositories.postgres import inbox as _postgres_inbox

    append_record = _postgres_inbox.append_record
    list_wal_space_ids = _postgres_inbox.list_wal_space_ids
    load_records = _postgres_inbox.load_records
    message_exists = _postgres_inbox.message_exists
    append_message_once = _postgres_inbox.append_message_once
    load_pending_records = _postgres_inbox.load_pending_records
    mark_processed = _postgres_inbox.mark_processed
    mark_sensitive_blocked = _postgres_inbox.mark_sensitive_blocked
