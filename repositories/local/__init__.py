"""Compatibility repository classes backed by the existing local stores."""

from __future__ import annotations

from typing import Any


class LocalInboxRepository:
    def append_once(self, record: Any) -> bool:
        """负责“追加once”。

        该函数是 `repositories.local.__init__` 中的`LocalInboxRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        from core.wal import append_message_once
        return append_message_once(record)

    def list_space_ids(self) -> list[str]:
        """负责“列出空间标识列表”。

        该函数是 `repositories.local.__init__` 中的`LocalInboxRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        from core.wal import list_wal_space_ids
        return list_wal_space_ids()

    def load(self, space_id: str) -> list[dict[str, Any]]:
        """负责“加载”。

        该函数是 `repositories.local.__init__` 中的`LocalInboxRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        from core.wal import load_records
        return load_records(space_id)

    def load_pending(self, space_id: str) -> list[dict[str, Any]]:
        """负责“加载待处理”。

        该函数是 `repositories.local.__init__` 中的`LocalInboxRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        from core.wal import load_pending_records
        return load_pending_records(space_id)

    def mark_processed(self, space_id: str, record_id: str) -> None:
        """负责“标记processed”。

        该函数是 `repositories.local.__init__` 中的`LocalInboxRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        from core.wal import mark_processed
        mark_processed(space_id, record_id)


class LocalNoteRepository:
    def save(self, note: Any) -> bool:
        """负责“保存”。

        该函数是 `repositories.local.__init__` 中的`LocalNoteRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        from storage.note_storage import note_exists, save_note
        existed = note_exists(note.space_id, note.message_id)
        save_note(note)
        return not existed

    def list(self, space_id: str) -> list[dict[str, Any]]:
        """负责“列出”。

        该函数是 `repositories.local.__init__` 中的`LocalNoteRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        from storage.note_storage import load_index
        return load_index(space_id)

    def find(self, space_id: str, note_id: str) -> dict[str, Any] | None:
        """负责“查找”。

        该函数是 `repositories.local.__init__` 中的`LocalNoteRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        from storage.note_storage import find_note
        return find_note(space_id, note_id)

    def update(self, space_id: str, note_id: str, **updates: Any) -> dict[str, Any] | None:
        """负责“更新”。

        该函数是 `repositories.local.__init__` 中的`LocalNoteRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        from storage.note_storage import update_note_metadata
        return update_note_metadata(space_id, note_id, **updates)
