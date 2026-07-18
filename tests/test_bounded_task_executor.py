from __future__ import annotations

import threading
import time

from runtime.executor import BoundedTaskExecutor
from runtime.task import TASK_FAILED, TASK_REJECTED, TASK_SUCCESS


def test_executor_rejects_when_queue_is_full(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    sent_messages = []

    def fake_process_record(record):
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
    enrichment_started = threading.Event()
    release_enrichment = threading.Event()
    archived_sent = threading.Event()

    monkeypatch.setattr("runtime.executor.process_record", lambda record: {"id": record["id"]})

    def fake_enrich(space_id, note_id):
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
    order = []
    monkeypatch.setattr("runtime.executor.process_pending", lambda space_id: order.append(("flush", space_id)) or 1)
    monkeypatch.setattr("runtime.executor.answer_question", lambda space_id, question: order.append(("answer", space_id)) or "ok")
    executor = BoundedTaskExecutor(max_workers=1, queue_size=1, send_text=lambda chat_id, text: True)

    task = executor.submit_query("s1", "刚才那条是什么", "chat1", "m1")
    executor.shutdown()

    assert task.status == TASK_SUCCESS
    assert order == [("flush", "s1"), ("answer", "s1")]
