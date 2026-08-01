"""文件作用：本地 repository 命名空间。

项目关系：本文件依赖 `core.wal`、`storage.note_storage`；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

from typing import Any


class LocalInboxRepository:
    """类功能：`LocalInboxRepository` 封装与“本地 repository 命名空间”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def append_once(self, record: Any) -> bool:
        """函数功能：`LocalInboxRepository.append_once` 在类 `LocalInboxRepository` 中负责追加 once，服务于本文件职责：本地 repository 命名空间。
        传参：
            record: 待处理或持久化的记录对象，类型为 `Any`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        from core.wal import append_message_once
        return append_message_once(record)

    def list_space_ids(self) -> list[str]:
        """函数功能：`LocalInboxRepository.list_space_ids` 在类 `LocalInboxRepository` 中负责列出 space ids，服务于本文件职责：本地 repository 命名空间。
        传参：
            无。
        返回结果说明：
            返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
        """
        from core.wal import list_wal_space_ids
        return list_wal_space_ids()

    def load(self, space_id: str) -> list[dict[str, Any]]:
        """函数功能：`LocalInboxRepository.load` 在类 `LocalInboxRepository` 中负责加载，服务于本文件职责：本地 repository 命名空间。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        返回结果说明：
            返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
        """
        from core.wal import load_records
        return load_records(space_id)

    def load_pending(self, space_id: str) -> list[dict[str, Any]]:
        """函数功能：`LocalInboxRepository.load_pending` 在类 `LocalInboxRepository` 中负责加载 pending，服务于本文件职责：本地 repository 命名空间。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        返回结果说明：
            返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
        """
        from core.wal import load_pending_records
        return load_pending_records(space_id)

    def mark_processed(self, space_id: str, record_id: str) -> None:
        """函数功能：`LocalInboxRepository.mark_processed` 在类 `LocalInboxRepository` 中负责标记 processed，服务于本文件职责：本地 repository 命名空间。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            record_id: record id 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        from core.wal import mark_processed
        mark_processed(space_id, record_id)


class LocalNoteRepository:
    """类功能：`LocalNoteRepository` 封装与“本地 repository 命名空间”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def save(self, note: Any) -> bool:
        """函数功能：`LocalNoteRepository.save` 在类 `LocalNoteRepository` 中负责保存，服务于本文件职责：本地 repository 命名空间。
        传参：
            note: note 参数，由调用方传入，类型为 `Any`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        from storage.note_storage import note_exists, save_note
        existed = note_exists(note.space_id, note.message_id)
        save_note(note)
        return not existed

    def list(self, space_id: str) -> list[dict[str, Any]]:
        """函数功能：`LocalNoteRepository.list` 在类 `LocalNoteRepository` 中负责列出，服务于本文件职责：本地 repository 命名空间。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        返回结果说明：
            返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
        """
        from storage.note_storage import load_index
        return load_index(space_id)

    def find(self, space_id: str, note_id: str) -> dict[str, Any] | None:
        """函数功能：`LocalNoteRepository.find` 在类 `LocalNoteRepository` 中负责查找，服务于本文件职责：本地 repository 命名空间。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            note_id: Note 标识，用于定位原始记录，类型为 `str`。
        返回结果说明：
            返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
        """
        from storage.note_storage import find_note
        return find_note(space_id, note_id)

    def update(self, space_id: str, note_id: str, **updates: Any) -> dict[str, Any] | None:
        """函数功能：`LocalNoteRepository.update` 在类 `LocalNoteRepository` 中负责更新，服务于本文件职责：本地 repository 命名空间。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            note_id: Note 标识，用于定位原始记录，类型为 `str`。
            **updates: updates 参数，由调用方传入，类型为 `Any`。
        返回结果说明：
            返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
        """
        from storage.note_storage import update_note_metadata
        return update_note_metadata(space_id, note_id, **updates)
