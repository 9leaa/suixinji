from datetime import datetime, timezone, timedelta
from summary import scheduler
from summary.subscription import SummarySubscription
from runtime.task import create_task

TZ = timezone(timedelta(hours=8))


def test_is_due_before_after_and_already_sent():
    sub = SummarySubscription(space_id="space1", chat_id="chat1", time="22:00")

    assert scheduler._is_due(sub, datetime(2026, 6, 7, 21, 59, tzinfo=TZ)) is False
    assert scheduler._is_due(sub, datetime(2026, 6, 7, 22, 0, tzinfo=TZ)) is True
    assert scheduler._is_due(sub, datetime(2026, 6, 7, 23, 30, tzinfo=TZ)) is True

    sent = SummarySubscription(
        space_id="space1",
        chat_id="chat1",
        time="22:00",
        last_sent_date="2026-06-07",
    )
    assert scheduler._is_due(sent, datetime(2026, 6, 7, 23, 30, tzinfo=TZ)) is False


def test_run_scheduler_once_sends_due_subscription(monkeypatch):
    sub = SummarySubscription(space_id="space1", chat_id="chat1", time="00:00")
    marked = []
    submitted = []
    fixed_now = datetime(2026, 6, 7, 23, 0, tzinfo=TZ)

    monkeypatch.setattr(scheduler, "list_enabled_summary_subscriptions", lambda: [sub])
    monkeypatch.setattr(scheduler, "mark_summary_sent", lambda space_id, day: marked.append((space_id, day)))

    class FakeExecutor:
        def submit_summary(self, space_id, range_key, chat_id, message_id=None, on_success=None, delivery_key=None, delivery_type=None):
            submitted.append((space_id, range_key, chat_id, message_id, delivery_key, delivery_type))
            if on_success is not None:
                on_success()
            return create_task("summary", space_id, {"range_key": range_key, "chat_id": chat_id})

    count = scheduler.run_summary_scheduler_once(lambda chat_id, text: True, executor=FakeExecutor(), now=fixed_now)

    assert count == 1
    today = fixed_now.date().isoformat()
    assert submitted == [("space1", "today", "chat1", None, f"auto_summary:space1:today:{today}", "auto_summary")]
    assert marked == [("space1", today)]


def test_run_scheduler_once_skips_before_configured_time(monkeypatch):
    sub = SummarySubscription(space_id="space1", chat_id="chat1", time="22:00")
    submitted = []

    monkeypatch.setattr(scheduler, "list_enabled_summary_subscriptions", lambda: [sub])

    class FakeExecutor:
        def submit_summary(self, *args, **kwargs):
            submitted.append((args, kwargs))
            raise AssertionError("should not submit before configured time")

    count = scheduler.run_summary_scheduler_once(
        lambda chat_id, text: True,
        executor=FakeExecutor(),
        now=datetime(2026, 6, 7, 21, 59, tzinfo=TZ),
    )

    assert count == 0
    assert submitted == []


def test_run_scheduler_once_sends_at_configured_time(monkeypatch):
    sub = SummarySubscription(space_id="space1", chat_id="chat1", time="22:00")
    submitted = []

    monkeypatch.setattr(scheduler, "list_enabled_summary_subscriptions", lambda: [sub])

    class FakeExecutor:
        def submit_summary(self, space_id, range_key, chat_id, message_id=None, on_success=None, delivery_key=None, delivery_type=None):
            submitted.append((space_id, range_key, chat_id, delivery_key, delivery_type))
            return create_task("summary", space_id, {})

    fixed_now = datetime(2026, 6, 7, 22, 0, tzinfo=TZ)
    count = scheduler.run_summary_scheduler_once(
        lambda chat_id, text: True,
        executor=FakeExecutor(),
        now=fixed_now,
    )

    assert count == 1
    assert submitted == [("space1", "today", "chat1", "auto_summary:space1:today:2026-06-07", "auto_summary")]


def test_run_scheduler_once_does_not_mark_when_task_is_rejected(monkeypatch):
    sub = SummarySubscription(space_id="space1", chat_id="chat1", time="00:00")
    marked = []

    monkeypatch.setattr(scheduler, "list_enabled_summary_subscriptions", lambda: [sub])
    monkeypatch.setattr(scheduler, "mark_summary_sent", lambda space_id, day: marked.append((space_id, day)))

    class FakeExecutor:
        def submit_summary(self, space_id, range_key, chat_id, message_id=None, on_success=None, delivery_key=None, delivery_type=None):
            task = create_task("summary", space_id, {"range_key": range_key, "chat_id": chat_id}, status="rejected")
            task.error = "task queue is full"
            return task

    count = scheduler.run_summary_scheduler_once(lambda chat_id, text: True, executor=FakeExecutor())

    assert count == 0
    assert marked == []
