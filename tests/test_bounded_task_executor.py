from __future__ import annotations

import threading

from runtime.executor import BoundedTaskExecutor
from runtime.task import TASK_REJECTED, TASK_SUCCESS


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
