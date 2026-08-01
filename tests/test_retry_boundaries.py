"""文件作用：可重试/不可重试错误边界。

项目关系：本文件依赖 `runtime`、`runtime.executor`、`runtime.retry`、`runtime.task`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from types import SimpleNamespace

import pytest

from runtime.executor import BoundedTaskExecutor
from runtime.retry import retry_external_call
from runtime.task import TASK_FAILED


def test_task_runner_failure_does_not_retry_whole_ingest(monkeypatch):
    """函数功能：`test_task_runner_failure_does_not_retry_whole_ingest` 负责验证 task runner failure does not retry whole ingest 场景，服务于本文件职责：可重试/不可重试错误边界。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    calls = []

    def fail_once(record):
        """函数功能：`fail_once` 负责处理 fail once，服务于本文件职责：可重试/不可重试错误边界。
        传参：
            record: 待处理或持久化的记录对象。
        返回结果说明：
            无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        calls.append(record["id"])
        raise RuntimeError("boom")

    monkeypatch.setattr("runtime.executor.process_record", fail_once)
    executor = BoundedTaskExecutor(max_workers=1, queue_size=1)

    task = executor.submit_ingest({"id": "r1", "space_id": "s1", "message_id": "m1"})
    executor.shutdown()

    assert calls == ["r1"]
    assert task.status == TASK_FAILED


def test_send_success_then_state_update_failure_does_not_resend(monkeypatch, tmp_path):
    """函数功能：`test_send_success_then_state_update_failure_does_not_resend` 负责验证 send success then state update failure does not resend 场景，服务于本文件职责：可重试/不可重试错误边界。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
        tmp_path: tmp path 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    from runtime import delivery_store

    monkeypatch.setattr(delivery_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(delivery_store, "DELIVERY_DIR", tmp_path / "deliveries")
    monkeypatch.setattr(delivery_store, "DELIVERY_PATH", tmp_path / "deliveries" / "index.json")
    monkeypatch.setattr(
        "runtime.executor.generate_summary",
        lambda space_id, range_key: SimpleNamespace(markdown="summary"),
    )
    sent = []

    def on_success():
        """函数功能：`on_success` 负责处理 on success，服务于本文件职责：可重试/不可重试错误边界。
        传参：
            无。
        返回结果说明：
            无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        raise RuntimeError("state update failed")

    executor = BoundedTaskExecutor(
        max_workers=1,
        queue_size=1,
        send_text=lambda chat_id, text: sent.append((chat_id, text)) or True,
    )

    task = executor.submit_summary("s1", "today", "chat1", message_id="m1", on_success=on_success)
    executor.shutdown()

    assert sent == [("chat1", "summary")]
    assert task.status == TASK_FAILED


def test_retry_external_call_uses_explicit_retryable_predicate():
    """函数功能：`test_retry_external_call_uses_explicit_retryable_predicate` 负责验证 retry external call uses explicit retryable predicate 场景，服务于本文件职责：可重试/不可重试错误边界。
    传参：
        无。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    calls = []

    def flaky():
        """函数功能：`flaky` 负责处理 flaky，服务于本文件职责：可重试/不可重试错误边界。
        传参：
            无。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
        calls.append("call")
        if len(calls) < 2:
            raise TimeoutError("temporary")
        return "ok"

    assert retry_external_call(flaky, max_retries=2, retryable=lambda exc: isinstance(exc, TimeoutError)) == "ok"
    assert calls == ["call", "call"]


def test_retry_external_call_does_not_retry_non_retryable_error():
    """函数功能：`test_retry_external_call_does_not_retry_non_retryable_error` 负责验证 retry external call does not retry non retryable error 场景，服务于本文件职责：可重试/不可重试错误边界。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    calls = []

    def bad_json():
        """函数功能：`bad_json` 负责处理 JSON 数据 bad，服务于本文件职责：可重试/不可重试错误边界。
        传参：
            无。
        返回结果说明：
            无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        calls.append("call")
        raise ValueError("invalid json")

    with pytest.raises(ValueError):
        retry_external_call(bad_json, max_retries=2, retryable=lambda exc: isinstance(exc, TimeoutError))

    assert calls == ["call"]
