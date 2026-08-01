"""文件作用：存储接口协议。

项目关系：本文件依赖 无直接本地模块依赖；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

from typing import Any, Protocol


class InboxRepository(Protocol):
    """类功能：`InboxRepository` 封装与“存储接口协议”相关的数据结构、状态或行为。
    继承关系：继承 `Protocol`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def append_once(self, record: Any) -> bool:
        """函数功能：`InboxRepository.append_once` 在类 `InboxRepository` 中负责追加 once，服务于本文件职责：存储接口协议。
        传参：
            record: 待处理或持久化的记录对象，类型为 `Any`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        ...
    def list_space_ids(self) -> list[str]:
        """函数功能：`InboxRepository.list_space_ids` 在类 `InboxRepository` 中负责列出 space ids，服务于本文件职责：存储接口协议。
        传参：
            无。
        返回结果说明：
            返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
        """
        ...
    def load(self, space_id: str) -> list[dict[str, Any]]:
        """函数功能：`InboxRepository.load` 在类 `InboxRepository` 中负责加载，服务于本文件职责：存储接口协议。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        返回结果说明：
            返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
        """
        ...
    def load_pending(self, space_id: str) -> list[dict[str, Any]]:
        """函数功能：`InboxRepository.load_pending` 在类 `InboxRepository` 中负责加载 pending，服务于本文件职责：存储接口协议。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        返回结果说明：
            返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
        """
        ...
    def mark_processed(self, space_id: str, record_id: str) -> None:
        """函数功能：`InboxRepository.mark_processed` 在类 `InboxRepository` 中负责标记 processed，服务于本文件职责：存储接口协议。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            record_id: record id 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        ...


class TaskRepository(Protocol):
    """类功能：`TaskRepository` 封装与“存储接口协议”相关的数据结构、状态或行为。
    继承关系：继承 `Protocol`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def create(self, task: dict[str, Any]) -> bool:
        """函数功能：`TaskRepository.create` 在类 `TaskRepository` 中负责创建，服务于本文件职责：存储接口协议。
        传参：
            task: task 参数，由调用方传入，类型为 `dict[str, Any]`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        ...
    def get(self, task_id: str) -> dict[str, Any] | None:
        """函数功能：`TaskRepository.get` 在类 `TaskRepository` 中负责获取，服务于本文件职责：存储接口协议。
        传参：
            task_id: 任务标识，用于查询、更新或幂等处理任务状态，类型为 `str`。
        返回结果说明：
            返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
        """
        ...
    def update_status(self, task_id: str, status: str, **updates: Any) -> None:
        """函数功能：`TaskRepository.update_status` 在类 `TaskRepository` 中负责更新 status，服务于本文件职责：存储接口协议。
        传参：
            task_id: 任务标识，用于查询、更新或幂等处理任务状态，类型为 `str`。
            status: status 参数，由调用方传入，类型为 `str`。
            **updates: updates 参数，由调用方传入，类型为 `Any`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        ...


class NoteRepository(Protocol):
    """类功能：`NoteRepository` 封装与“存储接口协议”相关的数据结构、状态或行为。
    继承关系：继承 `Protocol`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def save(self, note: Any) -> bool:
        """函数功能：`NoteRepository.save` 在类 `NoteRepository` 中负责保存，服务于本文件职责：存储接口协议。
        传参：
            note: note 参数，由调用方传入，类型为 `Any`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        ...
    def list(self, space_id: str) -> list[dict[str, Any]]:
        """函数功能：`NoteRepository.list` 在类 `NoteRepository` 中负责列出，服务于本文件职责：存储接口协议。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        返回结果说明：
            返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
        """
        ...
    def find(self, space_id: str, note_id: str) -> dict[str, Any] | None:
        """函数功能：`NoteRepository.find` 在类 `NoteRepository` 中负责查找，服务于本文件职责：存储接口协议。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            note_id: Note 标识，用于定位原始记录，类型为 `str`。
        返回结果说明：
            返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
        """
        ...
    def update(self, space_id: str, note_id: str, **updates: Any) -> dict[str, Any] | None:
        """函数功能：`NoteRepository.update` 在类 `NoteRepository` 中负责更新，服务于本文件职责：存储接口协议。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            note_id: Note 标识，用于定位原始记录，类型为 `str`。
            **updates: updates 参数，由调用方传入，类型为 `Any`。
        返回结果说明：
            返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
        """
        ...


class VectorRepository(Protocol):
    """类功能：`VectorRepository` 封装与“存储接口协议”相关的数据结构、状态或行为。
    继承关系：继承 `Protocol`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def add(self, space_id: str, item: Any) -> bool:
        """函数功能：`VectorRepository.add` 在类 `VectorRepository` 中负责处理 add，服务于本文件职责：存储接口协议。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            item: item 参数，由调用方传入，类型为 `Any`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        ...
    def list(self, space_id: str) -> list[Any]:
        """函数功能：`VectorRepository.list` 在类 `VectorRepository` 中负责列出，服务于本文件职责：存储接口协议。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        返回结果说明：
            返回 `list[Any]`，表示按条件筛选、构造或查询得到的列表。
        """
        ...
    def remove(self, space_id: str, note_id: str) -> bool:
        """函数功能：`VectorRepository.remove` 在类 `VectorRepository` 中负责移除，服务于本文件职责：存储接口协议。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            note_id: Note 标识，用于定位原始记录，类型为 `str`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        ...


class MemoryRepository(Protocol):
    """类功能：`MemoryRepository` 封装与“存储接口协议”相关的数据结构、状态或行为。
    继承关系：继承 `Protocol`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def get(self, memory_id: str) -> Any | None:
        """函数功能：`MemoryRepository.get` 在类 `MemoryRepository` 中负责获取，服务于本文件职责：存储接口协议。
        传参：
            memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        返回结果说明：
            返回 `Any | None`；未命中或无需处理时可返回 `None`。
        """
        ...
    def list(self, space_id: str, **filters: Any) -> list[Any]:
        """函数功能：`MemoryRepository.list` 在类 `MemoryRepository` 中负责列出，服务于本文件职责：存储接口协议。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            **filters: filters 参数，由调用方传入，类型为 `Any`。
        返回结果说明：
            返回 `list[Any]`，表示按条件筛选、构造或查询得到的列表。
        """
        ...
    def insert(self, space_id: str, candidate: Any, *, source_note_id: str, **options: Any) -> Any:
        """函数功能：`MemoryRepository.insert` 在类 `MemoryRepository` 中负责处理 insert，服务于本文件职责：存储接口协议。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
            candidate: candidate 参数，由调用方传入，类型为 `Any`。
            source_note_id: source note id 参数，由调用方传入，类型为 `str`。
            **options: options 参数，由调用方传入，类型为 `Any`。
        返回结果说明：
            返回 `Any` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        ...


class DeliveryRepository(Protocol):
    """类功能：`DeliveryRepository` 封装与“存储接口协议”相关的数据结构、状态或行为。
    继承关系：继承 `Protocol`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def reserve(self, delivery_key: str, **values: Any) -> Any | None:
        """函数功能：`DeliveryRepository.reserve` 在类 `DeliveryRepository` 中负责预约，服务于本文件职责：存储接口协议。
        传参：
            delivery_key: delivery key 参数，由调用方传入，类型为 `str`。
            **values: values 参数，由调用方传入，类型为 `Any`。
        返回结果说明：
            返回 `Any | None`；未命中或无需处理时可返回 `None`。
        """
        ...
    def get(self, delivery_key: str) -> Any | None:
        """函数功能：`DeliveryRepository.get` 在类 `DeliveryRepository` 中负责获取，服务于本文件职责：存储接口协议。
        传参：
            delivery_key: delivery key 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            返回 `Any | None`；未命中或无需处理时可返回 `None`。
        """
        ...
    def update_status(self, delivery_key: str, status: str, error: str | None = None) -> None:
        """函数功能：`DeliveryRepository.update_status` 在类 `DeliveryRepository` 中负责更新 status，服务于本文件职责：存储接口协议。
        传参：
            delivery_key: delivery key 参数，由调用方传入，类型为 `str`。
            status: status 参数，由调用方传入，类型为 `str`。
            error: 当前捕获的异常对象，类型为 `str | None`，默认值为 `None`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        ...


class SummaryRepository(Protocol):
    """类功能：`SummaryRepository` 封装与“存储接口协议”相关的数据结构、状态或行为。
    继承关系：继承 `Protocol`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def get(self, space_id: str) -> Any | None:
        """函数功能：`SummaryRepository.get` 在类 `SummaryRepository` 中负责获取，服务于本文件职责：存储接口协议。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        返回结果说明：
            返回 `Any | None`；未命中或无需处理时可返回 `None`。
        """
        ...
    def list_enabled(self) -> list[Any]:
        """函数功能：`SummaryRepository.list_enabled` 在类 `SummaryRepository` 中负责列出 enabled，服务于本文件职责：存储接口协议。
        传参：
            无。
        返回结果说明：
            返回 `list[Any]`，表示按条件筛选、构造或查询得到的列表。
        """
        ...
    def upsert(self, subscription: Any) -> Any:
        """函数功能：`SummaryRepository.upsert` 在类 `SummaryRepository` 中负责插入或更新，服务于本文件职责：存储接口协议。
        传参：
            subscription: subscription 参数，由调用方传入，类型为 `Any`。
        返回结果说明：
            返回 `Any` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        ...
