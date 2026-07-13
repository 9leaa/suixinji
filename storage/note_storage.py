"""Markdown note and index.json persistence helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from core.file_lock import locked_space, safe_space_id


DATA_DIR = Path("data")
NOTES_DIR = DATA_DIR / "notes"
@dataclass
class NoteMetadata:
    """表示一条已分类笔记的完整元数据。

    功能说明:
        承接 worker 处理后的结构化结果，用于写入 markdown 正文和 index.json 索引。

    传参说明:
        id: 本系统生成的笔记/WAL 记录 ID。
        message_id: 平台消息 ID。
        space_id: 会话/用户隔离 ID。
        ts: 笔记时间，ISO 格式字符串。
        title: 笔记标题。
        tags: 笔记标签列表。
        type: 笔记主类型。
        summary: 笔记摘要。
        text: 原始消息正文。
        related: 相关笔记 ID 列表，P2 RAG 阶段使用。

    返回类型说明:
        NoteMetadata: 一条可持久化的笔记元数据实例。
    """

    id: str
    message_id: str
    space_id: str
    ts: str
    title: str
    tags: list[str]
    type: str
    summary: str
    text: str
    related: list[str]


def note_dir(space_id: str) -> Path:
    """获取指定 space_id 对应的笔记目录。

    功能说明:
        根据 space_id 返回 `data/notes/{space_id}/` 目录，目录不存在时自动创建。

    传参说明:
        space_id: 会话/用户隔离 ID。

    返回类型说明:
        Path: 当前 space_id 对应的笔记目录路径。
    """
    path = NOTES_DIR / safe_space_id(space_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def note_date(ts: str) -> str:
    """从 ISO 时间字符串中提取日期。

    功能说明:
        将完整时间戳转换成日期字符串，用于决定每天的 markdown 文件名。

    传参说明:
        ts: ISO 格式时间字符串，例如 "2026-05-27T15:00:00+08:00"。

    返回类型说明:
        str: 日期字符串，格式为 "YYYY-MM-DD"。
    """
    return datetime.fromisoformat(ts).date().isoformat()


def note_file_path(space_id: str, ts: str) -> Path:
    """获取某条笔记应该写入的 markdown 文件路径。

    功能说明:
        根据 space_id 和时间戳生成当天 markdown 文件路径。

    传参说明:
        space_id: 会话/用户隔离 ID。
        ts: ISO 格式时间字符串，用于决定日期文件名。

    返回类型说明:
        Path: 对应日期的 markdown 笔记文件路径。
    """
    return note_dir(space_id) / f"{note_date(ts)}.md"


def index_path(space_id: str) -> Path:
    """获取指定 space_id 对应的 index.json 路径。

    功能说明:
        返回当前 space_id 的笔记索引文件路径。

    传参说明:
        space_id: 会话/用户隔离 ID。

    返回类型说明:
        Path: 当前 space_id 的索引文件路径。
    """
    return note_dir(space_id) / "index.json"


def load_index(space_id: str) -> list[dict[str, Any]]:
    """读取指定 space_id 的笔记索引。

    功能说明:
        从 index.json 中读取笔记元数据列表；如果索引文件不存在，则返回空列表。

    传参说明:
        space_id: 会话/用户隔离 ID。

    返回类型说明:
        list[dict[str, Any]]: index.json 中的笔记元数据列表；文件不存在时返回空列表。
    """
    path = index_path(space_id)
    with locked_space(space_id):
        if not path.exists():
            return []

        with path.open("r", encoding="utf-8") as f:
            return json.load(f)


def note_exists(space_id: str, message_id: str) -> bool:
    """判断指定 message_id 是否已经存在于笔记索引中。

    功能说明:
        扫描当前 space_id 的 index.json，检查是否已有相同 message_id 的笔记，
        用于 worker 崩溃恢复时避免重复写入 markdown 和索引。

    传参说明:
        space_id: 会话/用户隔离 ID。
        message_id: 平台消息 ID。

    返回类型说明:
        bool: 如果索引中已存在该 message_id，返回 True；否则返回 False。
    """
    return any(item.get("message_id") == message_id for item in load_index(space_id))


def save_index(space_id: str, items: list[dict[str, Any]]) -> None:
    """保存指定 space_id 的笔记索引。

    功能说明:
        将笔记元数据列表序列化为 JSON，并覆盖写入 index.json。

    传参说明:
        space_id: 会话/用户隔离 ID。
        items: 需要写入 index.json 的笔记元数据列表。

    返回类型说明:
        None: 该函数只执行文件写入，不返回业务结果。
    """
    path = index_path(space_id)
    with locked_space(space_id):
        with path.open("w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)


def append_markdown_note(meta: NoteMetadata) -> None:
    """将一条笔记追加写入当天的 markdown 文件。

    功能说明:
        将 NoteMetadata 格式化为可读 markdown 片段，并追加到对应日期的笔记文件。

    传参说明:
        meta: 需要写入 markdown 的笔记元数据。

    返回类型说明:
        None: 该函数只执行 markdown 文件追加写入，不返回业务结果。
    """
    path = note_file_path(meta.space_id, meta.ts)

    tags = " ".join(f"#{tag}" for tag in meta.tags)
    related = " ".join(f"`{note_id}`" for note_id in meta.related) or "无"

    block = f"""
## {meta.title}

- id: `{meta.id}`
- message_id: `{meta.message_id}`
- time: {meta.ts}
- type: {meta.type}
- tags: {tags}
- related: {related}

**摘要**：{meta.summary}

**原文**

{meta.text}

---
""".lstrip()

    with locked_space(meta.space_id):
        with path.open("a", encoding="utf-8") as f:
            f.write(block)


def append_index(meta: NoteMetadata) -> None:
    """将一条笔记元数据追加到 index.json。

    功能说明:
        读取现有索引，将当前笔记元数据追加到列表末尾，然后写回 index.json。

    传参说明:
        meta: 需要追加到索引中的笔记元数据。

    返回类型说明:
        None: 该函数只更新 index.json，不返回业务结果。
    """
    with locked_space(meta.space_id):
        items = load_index(meta.space_id)
        items.append(asdict(meta))
        save_index(meta.space_id, items)


def save_note(meta: NoteMetadata) -> None:
    """保存一条笔记到 markdown 文件和 index.json 索引。

    功能说明:
        组合调用 append_markdown_note 和 append_index，同时保存人类可读笔记和机器可读索引。
        保存前会检查 index.json 中是否已经存在相同 message_id，避免崩溃恢复时重复写入。

    传参说明:
        meta: 需要持久化保存的笔记元数据。

    返回类型说明:
        None: 该函数通过写入 markdown 和 index.json 完成保存，不返回业务结果。
    """
    with locked_space(meta.space_id):
        if note_exists(meta.space_id, meta.message_id):
            return

        append_markdown_note(meta)
        append_index(meta)
