"""文件作用：WAL pending 定时补偿。

项目关系：本文件依赖 `runtime.pending_drainer`、`runtime.task`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from runtime.pending_drainer import PendingDrainer
from runtime.task import TASK_REJECTED, create_task


class FakeExecutor:
    """类功能：`FakeExecutor` 封装与“WAL pending 定时补偿”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(self, rejected_after=None):
        """函数功能：`FakeExecutor.__init__` 在类 `FakeExecutor` 中负责初始化实例状态，服务于本文件职责：WAL pending 定时补偿。
        传参：
            rejected_after: rejected after 参数，由调用方传入，默认值为 `None`。
        返回结果说明：
            无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.rejected_after = rejected_after
        self.submitted = []
        self.inflight = set()

    def has_inflight_ingest(self, space_id, message_id):
        """函数功能：`FakeExecutor.has_inflight_ingest` 在类 `FakeExecutor` 中负责判断是否包含 inflight ingest，服务于本文件职责：WAL pending 定时补偿。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据。
            message_id: 外部或本地消息标识，用于入口幂等和追踪。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
        return (space_id, message_id) in self.inflight

    def submit_ingest(self, record, chat_id=None, notify_on_success=False, source="direct"):
        """函数功能：`FakeExecutor.submit_ingest` 在类 `FakeExecutor` 中负责处理 submit ingest，服务于本文件职责：WAL pending 定时补偿。
        传参：
            record: 待处理或持久化的记录对象。
            chat_id: chat id 参数，由调用方传入，默认值为 `None`。
            notify_on_success: notify on success 参数，由调用方传入，默认值为 `False`。
            source: source 参数，由调用方传入，默认值为 `'direct'`。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
        if self.rejected_after is not None and len(self.submitted) >= self.rejected_after:
            return create_task("ingest", record["space_id"], {}, message_id=record.get("message_id"), status=TASK_REJECTED)
        self.submitted.append((record, chat_id, notify_on_success, source))
        return create_task("ingest", record["space_id"], {}, message_id=record.get("message_id"))


def test_pending_drainer_submits_pending_without_success_notification(monkeypatch):
    """函数功能：`test_pending_drainer_submits_pending_without_success_notification` 负责验证 pending drainer submits pending without success notification 场景，服务于本文件职责：WAL pending 定时补偿。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    records = [{"id": "r1", "space_id": "s1", "message_id": "m1"}]
    executor = FakeExecutor()

    monkeypatch.setattr("runtime.pending_drainer.list_wal_space_ids", lambda: ["s1"])
    monkeypatch.setattr("runtime.pending_drainer.load_pending_records", lambda space_id: records)

    count = PendingDrainer(executor, batch_size=20).drain_once()

    assert count == 1
    assert executor.submitted == [(records[0], None, False, "pending_drainer")]


def test_pending_drainer_skips_inflight_message(monkeypatch):
    """函数功能：`test_pending_drainer_skips_inflight_message` 负责验证 pending drainer skips inflight message 场景，服务于本文件职责：WAL pending 定时补偿。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    records = [{"id": "r1", "space_id": "s1", "message_id": "m1"}]
    executor = FakeExecutor()
    executor.inflight.add(("s1", "m1"))

    monkeypatch.setattr("runtime.pending_drainer.list_wal_space_ids", lambda: ["s1"])
    monkeypatch.setattr("runtime.pending_drainer.load_pending_records", lambda space_id: records)

    assert PendingDrainer(executor).drain_once() == 0
    assert executor.submitted == []


def test_pending_drainer_respects_batch_size_and_stops_on_rejection(monkeypatch):
    """函数功能：`test_pending_drainer_respects_batch_size_and_stops_on_rejection` 负责验证 pending drainer respects batch size and stops on rejection 场景，服务于本文件职责：WAL pending 定时补偿。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    records = [
        {"id": f"r{index}", "space_id": "s1", "message_id": f"m{index}"}
        for index in range(5)
    ]
    executor = FakeExecutor(rejected_after=2)

    monkeypatch.setattr("runtime.pending_drainer.list_wal_space_ids", lambda: ["s1"])
    monkeypatch.setattr("runtime.pending_drainer.load_pending_records", lambda space_id: records)

    assert PendingDrainer(executor, batch_size=4).drain_once() == 2
    assert len(executor.submitted) == 2
