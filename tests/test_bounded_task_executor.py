"""文件作用：本地有界队列、拒绝和任务状态。

项目关系：本文件依赖 `runtime.executor`、`runtime.task`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from __future__ import annotations

import threading
import time

from runtime.executor import BoundedTaskExecutor
from runtime.task import TASK_FAILED, TASK_REJECTED, TASK_SUCCESS


def test_executor_rejects_when_queue_is_full(monkeypatch):
    """函数功能：`test_executor_rejects_when_queue_is_full` 负责验证 executor rejects when queue is full 场景，服务于本文件职责：本地有界队列、拒绝和任务状态。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    started = threading.Event()
    release = threading.Event()
    sent_messages = []

    def fake_process_record(record):
        """函数功能：`fake_process_record` 负责处理 record，服务于本文件职责：本地有界队列、拒绝和任务状态。
        传参：
            record: 待处理或持久化的记录对象。
        返回结果说明：
            无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr("runtime.executor.process_record", fake_process_record)
    executor = BoundedTaskExecutor(
        max_workers=1,
        queue_size=0,
        send_text=lambda chat_id, text: sent_messages.append((chat_id, text)) or True,
    )

    try:
        first = executor.submit_ingest(
            {"id": "r1", "space_id": "s1", "message_id": "m1"},
            "chat1",
        )
        assert started.wait(timeout=5)

        second = executor.submit_query("s1", "question", "chat1", "m2")

        assert first.status != TASK_REJECTED
        assert second.status == TASK_REJECTED
        assert "queue is full" in (second.error or "")
        assert executor.get_stats()["rejected"] == 1
    finally:
        release.set()
        executor.shutdown()


def test_executor_runs_ingest_and_updates_stats(monkeypatch):
    """函数功能：`test_executor_runs_ingest_and_updates_stats` 负责验证 executor runs ingest and updates stats 场景，服务于本文件职责：本地有界队列、拒绝和任务状态。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    processed = []
    sent_messages = []

    monkeypatch.setattr("runtime.executor.process_record", lambda record: processed.append(record))
    executor = BoundedTaskExecutor(
        max_workers=1,
        queue_size=1,
        send_text=lambda chat_id, text: sent_messages.append((chat_id, text)) or True,
    )

    try:
        task = executor.submit_ingest(
            {"id": "r1", "space_id": "s1", "message_id": "m1"},
            "chat1",
            notify_on_success=True,
        )
        executor.shutdown()

        assert task.status == TASK_SUCCESS
        assert processed == [{"id": "r1", "space_id": "s1", "message_id": "m1"}]
        assert sent_messages == [("chat1", "已归档到随心记。")]
        assert executor.get_stats()["success"] == 1
    finally:
        if task.status != TASK_SUCCESS:
            executor.shutdown()


def test_query_failure_sends_visible_notice(monkeypatch):
    """函数功能：`test_query_failure_sends_visible_notice` 负责验证 query failure sends visible notice 场景，服务于本文件职责：本地有界队列、拒绝和任务状态。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    sent_messages = []
    monkeypatch.setattr("runtime.executor.answer_question", lambda space_id, question: (_ for _ in ()).throw(RuntimeError("llm empty")))
    executor = BoundedTaskExecutor(
        max_workers=1,
        queue_size=1,
        send_text=lambda chat_id, text: sent_messages.append((chat_id, text)) or True,
    )

    task = executor.submit_query("s1", "我讨厌喝什么？", "chat1", "m1")
    executor.shutdown()

    assert task.status == TASK_FAILED
    assert sent_messages == [("chat1", "这次查询失败了，可能是模型暂时没有返回内容。请稍后再问一次。")]


def test_ingest_ack_does_not_wait_for_background_enrichment(monkeypatch):
    """函数功能：`test_ingest_ack_does_not_wait_for_background_enrichment` 负责验证 ingest ack does not wait for background enrichment 场景，服务于本文件职责：本地有界队列、拒绝和任务状态。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    enrichment_started = threading.Event()
    release_enrichment = threading.Event()
    archived_sent = threading.Event()

    monkeypatch.setattr("runtime.executor.process_record", lambda record: {"id": record["id"]})

    def fake_enrich(space_id, note_id):
        """函数功能：`fake_enrich` 负责处理 fake enrich，服务于本文件职责：本地有界队列、拒绝和任务状态。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据。
            note_id: Note 标识，用于定位原始记录。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
        enrichment_started.set()
        release_enrichment.wait(timeout=5)
        return True

    monkeypatch.setattr("runtime.executor.enrich_note", fake_enrich)
    executor = BoundedTaskExecutor(
        max_workers=1,
        queue_size=1,
        send_text=lambda chat_id, text: archived_sent.set() or True,
    )
    try:
        task = executor.submit_ingest(
            {"id": "r1", "space_id": "s1", "message_id": "m1"},
            "chat1",
            notify_on_success=True,
        )
        assert archived_sent.wait(timeout=5)
        assert enrichment_started.wait(timeout=5)
        for _ in range(100):
            if task.status == TASK_SUCCESS:
                break
            time.sleep(0.01)
        assert task.status == TASK_SUCCESS
    finally:
        release_enrichment.set()
        executor.shutdown()


def test_query_flushes_pending_wal_before_reading(monkeypatch):
    """函数功能：`test_query_flushes_pending_wal_before_reading` 负责验证 query flushes pending wal before reading 场景，服务于本文件职责：本地有界队列、拒绝和任务状态。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    order = []
    monkeypatch.setattr("runtime.executor.process_pending", lambda space_id: order.append(("flush", space_id)) or 1)
    monkeypatch.setattr("runtime.executor.answer_question", lambda space_id, question: order.append(("answer", space_id)) or "ok")
    executor = BoundedTaskExecutor(max_workers=1, queue_size=1, send_text=lambda chat_id, text: True)

    task = executor.submit_query("s1", "刚才那条是什么", "chat1", "m1")
    executor.shutdown()

    assert task.status == TASK_SUCCESS
    assert order == [("flush", "s1"), ("answer", "s1")]
