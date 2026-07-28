"""Behavioral contracts shared by local and PostgreSQL repositories."""

from __future__ import annotations

from typing import Any, Protocol


class InboxRepository(Protocol):
    def append_once(self, record: Any) -> bool:
        """负责“追加once”。

        该函数是 `repositories.interfaces` 中的`InboxRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...
    def list_space_ids(self) -> list[str]:
        """负责“列出空间标识列表”。

        该函数是 `repositories.interfaces` 中的`InboxRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...
    def load(self, space_id: str) -> list[dict[str, Any]]:
        """负责“加载”。

        该函数是 `repositories.interfaces` 中的`InboxRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...
    def load_pending(self, space_id: str) -> list[dict[str, Any]]:
        """负责“加载待处理”。

        该函数是 `repositories.interfaces` 中的`InboxRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...
    def mark_processed(self, space_id: str, record_id: str) -> None:
        """负责“标记processed”。

        该函数是 `repositories.interfaces` 中的`InboxRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...


class TaskRepository(Protocol):
    def create(self, task: dict[str, Any]) -> bool:
        """负责“创建”。

        该函数是 `repositories.interfaces` 中的`TaskRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...
    def get(self, task_id: str) -> dict[str, Any] | None:
        """负责“获取”。

        该函数是 `repositories.interfaces` 中的`TaskRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...
    def update_status(self, task_id: str, status: str, **updates: Any) -> None:
        """负责“更新状态”。

        该函数是 `repositories.interfaces` 中的`TaskRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...


class NoteRepository(Protocol):
    def save(self, note: Any) -> bool:
        """负责“保存”。

        该函数是 `repositories.interfaces` 中的`NoteRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...
    def list(self, space_id: str) -> list[dict[str, Any]]:
        """负责“列出”。

        该函数是 `repositories.interfaces` 中的`NoteRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...
    def find(self, space_id: str, note_id: str) -> dict[str, Any] | None:
        """负责“查找”。

        该函数是 `repositories.interfaces` 中的`NoteRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...
    def update(self, space_id: str, note_id: str, **updates: Any) -> dict[str, Any] | None:
        """负责“更新”。

        该函数是 `repositories.interfaces` 中的`NoteRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...


class VectorRepository(Protocol):
    def add(self, space_id: str, item: Any) -> bool:
        """负责“添加”。

        该函数是 `repositories.interfaces` 中的`VectorRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...
    def list(self, space_id: str) -> list[Any]:
        """负责“列出”。

        该函数是 `repositories.interfaces` 中的`VectorRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...
    def remove(self, space_id: str, note_id: str) -> bool:
        """负责“移除”。

        该函数是 `repositories.interfaces` 中的`VectorRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...


class MemoryRepository(Protocol):
    def get(self, memory_id: str) -> Any | None:
        """负责“获取”。

        该函数是 `repositories.interfaces` 中的`MemoryRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...
    def list(self, space_id: str, **filters: Any) -> list[Any]:
        """负责“列出”。

        该函数是 `repositories.interfaces` 中的`MemoryRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...
    def insert(self, space_id: str, candidate: Any, *, source_note_id: str, **options: Any) -> Any:
        """负责“插入”。

        该函数是 `repositories.interfaces` 中的`MemoryRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...


class DeliveryRepository(Protocol):
    def reserve(self, delivery_key: str, **values: Any) -> Any | None:
        """负责“预约”。

        该函数是 `repositories.interfaces` 中的`DeliveryRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...
    def get(self, delivery_key: str) -> Any | None:
        """负责“获取”。

        该函数是 `repositories.interfaces` 中的`DeliveryRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...
    def update_status(self, delivery_key: str, status: str, error: str | None = None) -> None:
        """负责“更新状态”。

        该函数是 `repositories.interfaces` 中的`DeliveryRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...


class SummaryRepository(Protocol):
    def get(self, space_id: str) -> Any | None:
        """负责“获取”。

        该函数是 `repositories.interfaces` 中的`SummaryRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...
    def list_enabled(self) -> list[Any]:
        """负责“列出enabled”。

        该函数是 `repositories.interfaces` 中的`SummaryRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...
    def upsert(self, subscription: Any) -> Any:
        """负责“新增或更新”。

        该函数是 `repositories.interfaces` 中的`SummaryRepository` 的方法；具体输入、输出和异常边界由类型标注及调用方约定。
        """
        ...
