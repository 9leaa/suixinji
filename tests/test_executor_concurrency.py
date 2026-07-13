import threading
import time

from runtime.executor import BoundedTaskExecutor
from runtime.task import TASK_REJECTED


def test_ingest_same_space_is_serialized_and_different_spaces_can_overlap(monkeypatch):
    active_by_space = {}
    max_active_same_space = {"s1": 0}
    seen_overlap = threading.Event()
    release = threading.Event()
    lock = threading.Lock()

    def fake_process_record(record):
        space_id = record["space_id"]
        with lock:
            active_by_space[space_id] = active_by_space.get(space_id, 0) + 1
            if space_id == "s1":
                max_active_same_space["s1"] = max(max_active_same_space["s1"], active_by_space[space_id])
            if len(active_by_space) >= 2:
                seen_overlap.set()
        release.wait(timeout=5)
        with lock:
            active_by_space[space_id] -= 1
            if active_by_space[space_id] == 0:
                del active_by_space[space_id]

    monkeypatch.setattr("runtime.executor.process_record", fake_process_record)
    executor = BoundedTaskExecutor(max_workers=3, queue_size=3)

    try:
        executor.submit_ingest({"id": "r1", "space_id": "s1", "message_id": "m1"})
        executor.submit_ingest({"id": "r2", "space_id": "s1", "message_id": "m2"})
        executor.submit_ingest({"id": "r3", "space_id": "s2", "message_id": "m3"})
        assert seen_overlap.wait(timeout=5)
    finally:
        release.set()
        executor.shutdown()

    assert max_active_same_space["s1"] == 1


def test_executor_pressure_limits_accepted_tasks(monkeypatch):
    started = 0
    max_started = 0
    lock = threading.Lock()

    def fake_answer(space_id, question):
        nonlocal started, max_started
        with lock:
            started += 1
            max_started = max(max_started, started)
        time.sleep(0.01)
        with lock:
            started -= 1
        return "answer"

    monkeypatch.setattr("runtime.executor.answer_question", fake_answer)
    executor = BoundedTaskExecutor(max_workers=4, queue_size=20, send_text=lambda chat_id, text: True)

    tasks = [
        executor.submit_query("s1", f"q{index}", "chat1", f"m{index}")
        for index in range(200)
    ]
    executor.shutdown()

    assert max_started <= 4
    assert sum(1 for task in tasks if task.status == TASK_REJECTED) >= 1
    assert all(task.status != "queued" and task.status != "running" for task in tasks)
    assert executor.get_stats()["retained_tasks"] <= 1000
