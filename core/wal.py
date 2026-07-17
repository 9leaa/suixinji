"""Write-ahead log helpers for append-only message ingestion."""
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
    """表示一条写入 WAL 的原始消息记录。

    功能说明:
        将飞书或其他入口收到的消息统一成项目内部 WAL 记录格式，
        供后续 worker 进行分类、存储和状态更新。

    传参说明:
        id: 本系统生成的唯一记录 ID。
        source: 消息来源平台，例如 "feishu"。
        event_id: 平台事件 ID，可为空。
        message_id: 平台消息 ID，用于消息去重。
        space_id: 本项目内部的会话/用户隔离 ID。
        chat_id: 平台群聊或会话 ID，可为空。
        chat_type: 会话类型，例如 "p2p"、"group" 或 "local"。
        sender: 发送者信息字典。
        ts: 消息进入系统的 ISO 格式时间字符串。
        text: 消息正文。
        status: WAL 处理状态，默认为 "pending"。

    返回类型说明:
        WalRecord: 一条可写入 WAL 的消息记录实例。
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
    """获取指定 space_id 对应的 WAL 文件路径。

    功能说明:
        根据 space_id 生成 `data/cache/{space_id}.jsonl` 路径，
        并在缓存目录不存在时自动创建目录。

    传参说明:
        space_id: 会话/用户隔离 ID。

    返回类型说明:
        Path: 当前 space_id 对应的 JSONL WAL 文件路径。
    """
    #如果父目录data不存在，也一起创建
    #如果存在则不报错
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{safe_space_id(space_id)}.jsonl"


def list_wal_space_ids() -> list[str]:
    """列出当前已有 WAL 文件对应的 space_id。

    功能说明:
        扫描 `data/cache/*.jsonl`，把每个 WAL 文件名去掉 `.jsonl` 后作为
        space_id 返回，供程序启动时恢复 pending 记录使用。

    传参说明:
        无参数。

    返回类型说明:
        list[str]: 已存在 WAL 文件对应的 space_id 列表；缓存目录不存在时返回空列表。
    """
    if not CACHE_DIR.exists():
        return []

    return sorted(path.stem for path in CACHE_DIR.glob("*.jsonl"))


def append_record(record: WalRecord) -> None:
    """将一条 WAL 记录追加写入 JSONL 文件。

    功能说明:
        把 WalRecord 转成字典，再序列化为一行 JSON，追加写入对应 space_id 的 WAL 文件。

    传参说明:
        record: 需要写入 WAL 的消息记录。

    返回类型说明:
        None: 该函数只执行文件追加写入，不返回业务结果。
    """
    path = wal_path(record.space_id)

    #参数a表示追加、asdict是将dataclass转成普通dict
    #然后dumps吧dict转为JSON字符串
    with locked_space(record.space_id):
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def load_records(space_id: str) -> list[dict[str, Any]]:
    """读取指定 space_id 下的所有 WAL 记录。

    功能说明:
        打开对应 space_id 的 JSONL 文件，逐行解析 JSON，返回全部 WAL 记录。
        如果文件不存在，则返回空列表。

    传参说明:
        space_id: 会话/用户隔离 ID。

    返回类型说明:
        list[dict[str, Any]]: 从 WAL 文件中解析出的记录列表；文件不存在时返回空列表。
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
    """判断指定 message_id 是否已经存在于 WAL 中。

    功能说明:
        用飞书 message_id 去重，而不是 event_id 去重。因为同一条消息可能会被
        飞书重复推送，但 message_id 应该保持一致。

    传参说明:
        space_id: 会话/用户隔离 ID。
        message_id: 平台消息 ID。

    返回类型说明:
        bool: 如果 WAL 中已存在该 message_id，返回 True；否则返回 False。
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
    """创建一条状态为 pending 的 WAL 记录。

    功能说明:
        根据平台消息信息创建统一的 WalRecord，并自动生成记录 ID、来源平台、
        当前时间和默认 pending 状态。

    传参说明:
        message_id: 平台消息 ID，用于去重。
        space_id: 会话/用户隔离 ID。
        text: 消息正文。
        event_id: 平台事件 ID，可为空。
        chat_id: 平台会话 ID，可为空。
        chat_type: 会话类型，默认是 "p2p"。
        sender: 发送者信息字典；为空时使用空字典。

    返回类型说明:
        WalRecord: 新创建的 pending 状态 WAL 记录。
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
    """Create a redacted idempotency record without persisting the secret."""
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
    """在消息未重复时追加写入 WAL。

    功能说明:
        先通过 message_id 检查同一 space_id 下是否已有相同消息，
        如果不存在才追加写入，避免重复事件导致重复记录。

    传参说明:
        record: 待写入的 WAL 记录。

    返回类型说明:
        bool: 成功追加时返回 True；检测到重复消息并跳过时返回 False。
    """
    with locked_space(record.space_id):
        if message_exists(record.space_id, record.message_id):
            return False

        append_record(record)
        return True


def load_pending_records(space_id: str) -> list[dict[str, Any]]:
    """读取指定 space_id 下所有待处理的 WAL 记录。

    功能说明:
        从 WAL 文件中筛选 `status` 为 "pending" 的记录，供 worker 后台处理。

    传参说明:
        space_id: 会话/用户隔离 ID。

    返回类型说明:
        list[dict[str, Any]]: `status` 为 "pending" 的 WAL 记录列表。
    """
    return [
        record
        for record in load_records(space_id)
        if record.get("status") == "pending"
    ]


def mark_processed(space_id: str, record_id: str) -> None:
    """将指定 WAL 记录的状态标记为 processed。

    功能说明:
        读取当前 space_id 的完整 WAL 文件，找到指定 record_id 的记录后将其
        status 改为 "processed"，再将全部记录重新写回文件。

    传参说明:
        space_id: 会话/用户隔离 ID。
        record_id: 本系统生成的 WAL 记录 ID。

    返回类型说明:
        None: 该函数只更新文件状态，不返回业务结果。
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


def mark_sensitive_blocked(space_id: str, record_id: str, category: str = "sensitive") -> None:
    """Redact a legacy pending record in place without retaining its raw text."""
    path = wal_path(space_id)
    with locked_space(space_id):
        records = load_records(space_id)
        for record in records:
            if record.get("id") != record_id:
                continue
            record["text"] = "[敏感内容已拦截，原文未保存]"
            record["status"] = "blocked_sensitive"
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
