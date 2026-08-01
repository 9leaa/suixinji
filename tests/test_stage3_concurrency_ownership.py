"""文件作用：Stage 3 并发所有权/租约。

项目关系：本文件依赖 `core.settings`、`memory`、`memory.models`、`runtime.streams.client` 等 5 个模块；被 暂无静态导入方或仅作为入口脚本执行。
"""


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
    """函数功能：`test_stage4_role_connection_plan_stays_within_global_budget` 负责验证 stage4 role connection plan stays within global budget 场景，服务于本文件职责：Stage 3 并发所有权/租约。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`test_stream_worker_reads_new_messages_before_periodic_reclaim` 负责验证 stream worker reads new messages before periodic reclaim 场景，服务于本文件职责：Stage 3 并发所有权/租约。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    new_message = StreamMessage("stream", "1-0", {"task_id": "new"})
    reclaimed_message = StreamMessage("stream", "0-1", {"task_id": "old"})

    class FakeClient:
        """类功能：`FakeClient` 封装与“Stage 3 并发所有权/租约”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def __init__(self):
            """函数功能：`FakeClient.__init__` 在类 `FakeClient` 中负责初始化实例状态，服务于本文件职责：Stage 3 并发所有权/租约。
            传参：
                无。
            返回结果说明：
                无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
            """
            self.read_results = [[new_message], []]
            self.reclaim_calls = 0

        def read(self, *_args, **_kwargs):
            """函数功能：`FakeClient.read` 在类 `FakeClient` 中负责读取，服务于本文件职责：Stage 3 并发所有权/租约。
            传参：
                *_args:  args 参数，由调用方传入。
                **_kwargs:  kwargs 参数，由调用方传入。
            返回结果说明：
                返回计算后的结果对象；具体类型取决于实际执行分支。
            """
            return self.read_results.pop(0)

        def reclaim(self, *_args, **_kwargs):
            """函数功能：`FakeClient.reclaim` 在类 `FakeClient` 中负责处理 reclaim，服务于本文件职责：Stage 3 并发所有权/租约。
            传参：
                *_args:  args 参数，由调用方传入。
                **_kwargs:  kwargs 参数，由调用方传入。
            返回结果说明：
                返回计算后的结果对象；具体类型取决于实际执行分支。
            """
            self.reclaim_calls += 1
            return [reclaimed_message]

        def reclaim_cursor(self, *_args):
            """函数功能：`FakeClient.reclaim_cursor` 在类 `FakeClient` 中负责处理 reclaim cursor，服务于本文件职责：Stage 3 并发所有权/租约。
            传参：
                *_args:  args 参数，由调用方传入。
            返回结果说明：
                返回计算后的结果对象；具体类型取决于实际执行分支。
            """
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
    """函数功能：`test_same_memory_key_evolution_is_mutually_exclusive` 负责验证 same memory key evolution is mutually exclusive 场景，服务于本文件职责：Stage 3 并发所有权/租约。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    active = 0
    max_active = 0
    guard = threading.Lock()

    def candidate(note_id: str) -> MemoryCandidate:
        """函数功能：`candidate` 负责处理 candidate，服务于本文件职责：Stage 3 并发所有权/租约。
        传参：
            note_id: Note 标识，用于定位原始记录，类型为 `str`。
        返回结果说明：
            返回 `MemoryCandidate` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
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
        """函数功能：`consolidate` 负责合并长期记忆，服务于本文件职责：Stage 3 并发所有权/租约。
        传参：
            _space_id:  space id 参数，由调用方传入。
            _note_id:  note id 参数，由调用方传入。
            memory_candidate: memory candidate 参数，由调用方传入。
            trace: trace 参数，由调用方传入，默认值为 `None`。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
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
