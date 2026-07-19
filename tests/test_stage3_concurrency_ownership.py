from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from core.settings import DATABASE_GLOBAL_BUDGET, database_pool_budget
from memory import service
from memory.models import MemoryCandidate
from runtime.streams.client import StreamMessage
from runtime.streams.worker import StreamWorker


def test_stage4_role_connection_plan_stays_within_global_budget() -> None:
    roles = {
        "receiver": 2,
        "outbox-relay": 2,
        "worker-ingest": 4,
        "worker-memory": 8,
        "worker-query": 2,
        "worker-summary": 2,
        "worker-enrichment": 2,
        "worker-delivery": 2,
        "scheduler": 2,
    }
    theoretical_peak = sum(count * sum(database_pool_budget(role)) for role, count in roles.items())
    assert theoretical_peak == 38
    assert theoretical_peak <= DATABASE_GLOBAL_BUDGET


def test_stream_worker_reads_new_messages_before_periodic_reclaim() -> None:
    new_message = StreamMessage("stream", "1-0", {"task_id": "new"})
    reclaimed_message = StreamMessage("stream", "0-1", {"task_id": "old"})

    class FakeClient:
        def __init__(self):
            self.read_results = [[new_message], []]
            self.reclaim_calls = 0

        def read(self, *_args, **_kwargs):
            return self.read_results.pop(0)

        def reclaim(self, *_args, **_kwargs):
            self.reclaim_calls += 1
            return [reclaimed_message]

        def reclaim_cursor(self, *_args):
            return "2-0"

    client = FakeClient()
    handled = []
    worker = StreamWorker("ingest", lambda _task: None, client=client, worker_id="reclaim-order")
    worker._handle = handled.append
    worker._next_reclaim_at = 0

    assert worker.run_once(block_ms=0) == 1
    assert handled == [new_message]
    assert client.reclaim_calls == 0
    assert worker.run_once(block_ms=0) == 1
    assert handled == [new_message, reclaimed_message]
    assert client.reclaim_calls == 1


def test_same_memory_key_evolution_is_mutually_exclusive(monkeypatch) -> None:
    active = 0
    max_active = 0
    guard = threading.Lock()

    def candidate(note_id: str) -> MemoryCandidate:
        return MemoryCandidate(
            "preference",
            "用户喜欢绿茶",
            0.8,
            0.9,
            note_id=note_id,
            memory_key="preference:user:tea",
        )

    monkeypatch.setattr(service, "contains_sensitive_data", lambda _text: False)
    monkeypatch.setattr(service, "get_extraction_state", lambda _note_id: None)
    monkeypatch.setattr(service, "mark_extraction_processing", lambda *_args: SimpleNamespace(attempt_count=1))
    monkeypatch.setattr(service, "mark_extraction_completed", lambda *_args, **_kwargs: SimpleNamespace(candidate_count=1, processed_count=1, attempt_count=1))
    monkeypatch.setattr(service, "save_memory_candidate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "mark_memory_candidate", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(service, "get_memory_candidate_status", lambda _candidate_id: None)
    monkeypatch.setattr(service, "extract_candidates", lambda note_id, _text, classification=None: [candidate(note_id)])
    monkeypatch.setattr(service, "validate_candidates", lambda candidates, note_text: (candidates, []))

    def consolidate(_space_id, _note_id, memory_candidate, trace=None):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return {"action": "insert", "decision_id": memory_candidate.candidate_id}

    monkeypatch.setattr(service, "consolidate_candidate", consolidate)
    notes = [
        {"id": "note-a", "space_id": "space-lock", "text": "我喜欢绿茶"},
        {"id": "note-b", "space_id": "space-lock", "text": "我喜欢绿茶"},
    ]
    threads = [threading.Thread(target=service._process_note_memory_impl, args=(note,)) for note in notes]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert max_active == 1
