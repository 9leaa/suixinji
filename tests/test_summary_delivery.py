"""文件作用：总结生成后 delivery 预约/发送。

项目关系：本文件依赖 `runtime`、`runtime.delivery_store`、`runtime.executor`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from types import SimpleNamespace

from runtime import delivery_store
from runtime.delivery_store import auto_summary_key
from runtime.executor import BoundedTaskExecutor


def isolate_delivery_store(monkeypatch, tmp_path):
    """函数功能：`isolate_delivery_store` 负责处理 isolate delivery store，服务于本文件职责：总结生成后 delivery 预约/发送。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
        tmp_path: tmp path 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(delivery_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(delivery_store, "DELIVERY_DIR", tmp_path / "deliveries")
    monkeypatch.setattr(delivery_store, "DELIVERY_PATH", tmp_path / "deliveries" / "index.json")


def test_auto_summary_delivery_is_sent_once(monkeypatch, tmp_path):
    """函数功能：`test_auto_summary_delivery_is_sent_once` 负责验证 auto summary delivery is sent once 场景，服务于本文件职责：总结生成后 delivery 预约/发送。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
        tmp_path: tmp path 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    isolate_delivery_store(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "runtime.executor.generate_summary",
        lambda space_id, range_key: SimpleNamespace(markdown=f"summary {space_id} {range_key}"),
    )
    sent = []
    marked = []
    key = auto_summary_key("s1", "today", "2026-07-13")

    executor = BoundedTaskExecutor(
        max_workers=1,
        queue_size=2,
        send_text=lambda chat_id, text: sent.append((chat_id, text)) or True,
    )

    executor.submit_summary("s1", "today", "chat1", delivery_key=key, delivery_type="auto_summary", on_success=lambda: marked.append("sent"))
    executor.shutdown()

    executor2 = BoundedTaskExecutor(
        max_workers=1,
        queue_size=2,
        send_text=lambda chat_id, text: sent.append((chat_id, text)) or True,
    )
    executor2.submit_summary("s1", "today", "chat1", delivery_key=key, delivery_type="auto_summary", on_success=lambda: marked.append("sent"))
    executor2.shutdown()

    assert sent == [("chat1", "summary s1 today")]
    assert marked == ["sent"]
