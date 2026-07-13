from runtime.pending_drainer import PendingDrainer
from runtime.task import TASK_REJECTED, create_task


class FakeExecutor:
    def __init__(self, rejected_after=None):
        self.rejected_after = rejected_after
        self.submitted = []
        self.inflight = set()

    def has_inflight_ingest(self, space_id, message_id):
        return (space_id, message_id) in self.inflight

    def submit_ingest(self, record, chat_id=None, notify_on_success=False, source="direct"):
        if self.rejected_after is not None and len(self.submitted) >= self.rejected_after:
            return create_task("ingest", record["space_id"], {}, message_id=record.get("message_id"), status=TASK_REJECTED)
        self.submitted.append((record, chat_id, notify_on_success, source))
        return create_task("ingest", record["space_id"], {}, message_id=record.get("message_id"))


def test_pending_drainer_submits_pending_without_success_notification(monkeypatch):
    records = [{"id": "r1", "space_id": "s1", "message_id": "m1"}]
    executor = FakeExecutor()

    monkeypatch.setattr("runtime.pending_drainer.list_wal_space_ids", lambda: ["s1"])
    monkeypatch.setattr("runtime.pending_drainer.load_pending_records", lambda space_id: records)

    count = PendingDrainer(executor, batch_size=20).drain_once()

    assert count == 1
    assert executor.submitted == [(records[0], None, False, "pending_drainer")]


def test_pending_drainer_skips_inflight_message(monkeypatch):
    records = [{"id": "r1", "space_id": "s1", "message_id": "m1"}]
    executor = FakeExecutor()
    executor.inflight.add(("s1", "m1"))

    monkeypatch.setattr("runtime.pending_drainer.list_wal_space_ids", lambda: ["s1"])
    monkeypatch.setattr("runtime.pending_drainer.load_pending_records", lambda space_id: records)

    assert PendingDrainer(executor).drain_once() == 0
    assert executor.submitted == []


def test_pending_drainer_respects_batch_size_and_stops_on_rejection(monkeypatch):
    records = [
        {"id": f"r{index}", "space_id": "s1", "message_id": f"m{index}"}
        for index in range(5)
    ]
    executor = FakeExecutor(rejected_after=2)

    monkeypatch.setattr("runtime.pending_drainer.list_wal_space_ids", lambda: ["s1"])
    monkeypatch.setattr("runtime.pending_drainer.load_pending_records", lambda space_id: records)

    assert PendingDrainer(executor, batch_size=4).drain_once() == 2
    assert len(executor.submitted) == 2
