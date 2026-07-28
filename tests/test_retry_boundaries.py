from types import SimpleNamespace

import pytest

from runtime.executor import BoundedTaskExecutor
from runtime.retry import retry_external_call
from runtime.task import TASK_FAILED


def test_task_runner_failure_does_not_retry_whole_ingest(monkeypatch):
    """验证“任务runnerfailuredoesnot重试whole接收写入”场景的预期行为与回归边界。"""
    calls = []

    def fail_once(record):
        """验证“失败once”场景的预期行为与回归边界。"""
        calls.append(record["id"])
        raise RuntimeError("boom")

    monkeypatch.setattr("runtime.executor.process_record", fail_once)
    executor = BoundedTaskExecutor(max_workers=1, queue_size=1)

    task = executor.submit_ingest({"id": "r1", "space_id": "s1", "message_id": "m1"})
    executor.shutdown()

    assert calls == ["r1"]
    assert task.status == TASK_FAILED


def test_send_success_then_state_update_failure_does_not_resend(monkeypatch, tmp_path):
    """验证“发送successthen状态更新failuredoesnotresend”场景的预期行为与回归边界。"""
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
        """验证“success”场景的预期行为与回归边界。"""
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
    """验证“重试externalcallusesexplicitretryablepredicate”场景的预期行为与回归边界。"""
    calls = []

    def flaky():
        """验证“flaky”场景的预期行为与回归边界。"""
        calls.append("call")
        if len(calls) < 2:
            raise TimeoutError("temporary")
        return "ok"

    assert retry_external_call(flaky, max_retries=2, retryable=lambda exc: isinstance(exc, TimeoutError)) == "ok"
    assert calls == ["call", "call"]


def test_retry_external_call_does_not_retry_non_retryable_error():
    """验证“重试externalcalldoesnot重试nonretryable错误”场景的预期行为与回归边界。"""
    calls = []

    def bad_json():
        """验证“badJSON”场景的预期行为与回归边界。"""
        calls.append("call")
        raise ValueError("invalid json")

    with pytest.raises(ValueError):
        retry_external_call(bad_json, max_retries=2, retryable=lambda exc: isinstance(exc, TimeoutError))

    assert calls == ["call"]
