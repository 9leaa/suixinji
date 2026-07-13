from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from summary import scheduler
from summary.subscription import SummarySubscription

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
    sent_messages = []
    marked = []

    monkeypatch.setattr(scheduler, "list_enabled_summary_subscriptions", lambda: [sub])
    monkeypatch.setattr(
        scheduler,
        "generate_summary",
        lambda space_id, range_key: SimpleNamespace(markdown=f"summary for {space_id}/{range_key}"),
    )
    monkeypatch.setattr(scheduler, "mark_summary_sent", lambda space_id, day: marked.append((space_id, day)))

    count = scheduler.run_summary_scheduler_once(lambda chat_id, text: sent_messages.append((chat_id, text)) or True)

    assert count == 1
    assert sent_messages == [("chat1", "summary for space1/today")]
    assert marked == [("space1", datetime.now().astimezone().date().isoformat())]


def test_run_scheduler_once_does_not_mark_when_send_fails(monkeypatch):
    sub = SummarySubscription(space_id="space1", chat_id="chat1", time="00:00")
    marked = []

    monkeypatch.setattr(scheduler, "list_enabled_summary_subscriptions", lambda: [sub])
    monkeypatch.setattr(
        scheduler,
        "generate_summary",
        lambda space_id, range_key: SimpleNamespace(markdown="summary"),
    )
    monkeypatch.setattr(scheduler, "mark_summary_sent", lambda space_id, day: marked.append((space_id, day)))

    count = scheduler.run_summary_scheduler_once(lambda chat_id, text: False)

    assert count == 0
    assert marked == []
