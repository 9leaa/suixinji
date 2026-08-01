"""文件作用：本地 Note 生命周期与检索。

项目关系：本文件依赖 `core.file_lock`、`core.sensitive`、`core.settings`、`repositories.postgres`；被 `agent.query_agent`、`apps.handlers`、`core.worker`、`memory.consolidator` 等 10 个模块。
"""



from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from core.file_lock import locked_space, safe_space_id
from core.sensitive import contains_sensitive_data


DATA_DIR = Path("data")
NOTES_DIR = DATA_DIR / "notes"
@dataclass
class NoteMetadata:
    """类功能：`NoteMetadata` 封装与“本地 Note 生命周期与检索”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
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
    enrichment_status: str = "ready"
    enrichment_attempts: int = 0
    enrichment_error: str | None = None
    enrichment_started_at: str | None = None
    enrichment_updated_at: str | None = None
    sensitivity: str = "normal"
    tenant_id: str = "default"


def note_dir(space_id: str) -> Path:
    """函数功能：`note_dir` 负责处理 note dir，服务于本文件职责：本地 Note 生命周期与检索。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `Path` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    path = NOTES_DIR / safe_space_id(space_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def note_date(ts: str) -> str:
    """函数功能：`note_date` 负责处理 note date，服务于本文件职责：本地 Note 生命周期与检索。
    传参：
        ts: ts 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return datetime.fromisoformat(ts).date().isoformat()


def note_file_path(space_id: str, ts: str) -> Path:
    """函数功能：`note_file_path` 负责处理 note file path，服务于本文件职责：本地 Note 生命周期与检索。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        ts: ts 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `Path` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return note_dir(space_id) / f"{note_date(ts)}.md"


def index_path(space_id: str) -> Path:
    """函数功能：`index_path` 负责处理 index path，服务于本文件职责：本地 Note 生命周期与检索。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `Path` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return note_dir(space_id) / "index.json"


def load_index(space_id: str) -> list[dict[str, Any]]:
    """函数功能：`load_index` 负责加载 index，服务于本文件职责：本地 Note 生命周期与检索。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    path = index_path(space_id)
    with locked_space(space_id):
        if not path.exists():
            return []

        with path.open("r", encoding="utf-8") as f:
            return json.load(f)


def list_note_space_ids() -> list[str]:
    """函数功能：`list_note_space_ids` 负责列出 note space ids，服务于本文件职责：本地 Note 生命周期与检索。
    传参：
        无。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
    if not NOTES_DIR.exists():
        return []
    return sorted(path.name for path in NOTES_DIR.iterdir() if path.is_dir())


def is_note_queryable(note: dict[str, Any]) -> bool:
    """函数功能：`is_note_queryable` 负责判断是否为 note queryable，服务于本文件职责：本地 Note 生命周期与检索。
    传参：
        note: note 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    sensitivity = str(note.get("sensitivity") or "normal").casefold()
    if sensitivity not in {"", "normal", "none"}:
        return False
    return not contains_sensitive_data(str(note.get("text") or ""))


def load_queryable_index(space_id: str) -> list[dict[str, Any]]:
    """函数功能：`load_queryable_index` 负责加载 queryable index，服务于本文件职责：本地 Note 生命周期与检索。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    return [note for note in load_index(space_id) if is_note_queryable(note)]


def find_note(space_id: str, note_id: str) -> dict[str, Any] | None:
    """函数功能：`find_note` 负责查找 note，服务于本文件职责：本地 Note 生命周期与检索。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
    返回结果说明：
        返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
    """
    for note in load_index(space_id):
        if str(note.get("id") or "") == str(note_id):
            return note
    return None


def update_note_metadata(space_id: str, note_id: str, **updates: Any) -> dict[str, Any] | None:
    """函数功能：`update_note_metadata` 负责更新 note metadata，服务于本文件职责：本地 Note 生命周期与检索。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        **updates: updates 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
    """
    with locked_space(space_id):
        items = load_index(space_id)
        updated: dict[str, Any] | None = None
        for item in items:
            if str(item.get("id") or "") != str(note_id):
                continue
            item.update(updates)
            updated = dict(item)
            break
        if updated is not None:
            save_index(space_id, items)
        return updated


def list_pending_enrichments(*, limit: int = 100, max_attempts: int = 3) -> list[dict[str, str]]:
    """函数功能：`list_pending_enrichments` 负责列出 pending enrichments，服务于本文件职责：本地 Note 生命周期与检索。
    传参：
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `100`。
        max_attempts: max attempts 参数，由调用方传入，类型为 `int`，默认值为 `3`。
    返回结果说明：
        返回 `list[dict[str, str]]`，表示按条件筛选、构造或查询得到的列表。
    """
    if not NOTES_DIR.exists():
        return []

    pending: list[dict[str, str]] = []
    for directory in sorted(path for path in NOTES_DIR.iterdir() if path.is_dir()):
        for note in load_index(directory.name):
            status = str(note.get("enrichment_status") or "ready")
            attempts = int(note.get("enrichment_attempts") or 0)
            if status not in {"provisional", "enriching", "failed"}:
                continue
            if attempts >= max_attempts:
                continue
            if not is_note_queryable(note):
                continue
            note_id = str(note.get("id") or "")
            space_id = str(note.get("space_id") or directory.name)
            if note_id and space_id:
                pending.append({"space_id": space_id, "note_id": note_id})
                if len(pending) >= max(1, int(limit)):
                    return pending
    return pending


def note_exists(space_id: str, message_id: str) -> bool:
    """函数功能：`note_exists` 负责处理 note exists，服务于本文件职责：本地 Note 生命周期与检索。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    return any(item.get("message_id") == message_id for item in load_index(space_id))


def save_index(space_id: str, items: list[dict[str, Any]]) -> None:
    """函数功能：`save_index` 负责保存 index，服务于本文件职责：本地 Note 生命周期与检索。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        items: 待遍历或处理的元素集合，类型为 `list[dict[str, Any]]`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    path = index_path(space_id)
    with locked_space(space_id):
        with path.open("w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)


def append_markdown_note(meta: NoteMetadata) -> None:
    """函数功能：`append_markdown_note` 负责追加 markdown note，服务于本文件职责：本地 Note 生命周期与检索。
    传参：
        meta: meta 参数，由调用方传入，类型为 `NoteMetadata`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
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
    """函数功能：`append_index` 负责追加 index，服务于本文件职责：本地 Note 生命周期与检索。
    传参：
        meta: meta 参数，由调用方传入，类型为 `NoteMetadata`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    with locked_space(meta.space_id):
        items = load_index(meta.space_id)
        items.append(asdict(meta))
        save_index(meta.space_id, items)


def save_note(meta: NoteMetadata) -> None:
    """函数功能：`save_note` 负责保存 note，服务于本文件职责：本地 Note 生命周期与检索。
    传参：
        meta: meta 参数，由调用方传入，类型为 `NoteMetadata`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    with locked_space(meta.space_id):
        if note_exists(meta.space_id, meta.message_id):
            return

        append_markdown_note(meta)
        append_index(meta)


from core.settings import STORAGE_BACKEND as _STORAGE_BACKEND

if _STORAGE_BACKEND == "postgres":
    from repositories.postgres import notes as _postgres_notes

    load_index = _postgres_notes.load_index
    list_note_space_ids = _postgres_notes.list_space_ids

    def load_queryable_index(space_id: str) -> list[dict[str, Any]]:
        """函数功能：`load_queryable_index` 负责加载 queryable index，服务于本文件职责：本地 Note 生命周期与检索。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        返回结果说明：
            返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
        """
        return [note for note in load_index(space_id) if is_note_queryable(note)]

    find_note = _postgres_notes.find_note
    update_note_metadata = _postgres_notes.update_note_metadata
    list_pending_enrichments = _postgres_notes.list_pending_enrichments
    note_exists = _postgres_notes.note_exists
    save_note = _postgres_notes.save_note
