import json
from datetime import datetime, timedelta, timezone

from runtime.delivery_store import (
    auto_summary_key,
    mark_failed,
    mark_sent,
    mark_unknown,
    reserve_delivery,
)
from runtime.task import create_task
from summary import reconciliation, scheduler, subscription

FIXED_NOW = datetime(2026, 7, 14, 23, 0, tzinfo=timezone.utc)


def isolate_subscription_file(monkeypatch, tmp_path):
    monkeypatch.setattr(subscription, "DATA_DIR", tmp_path)
    monkeypatch.setattr(subscription, "SUBSCRIPTIONS_PATH", tmp_path / "summary_subscriptions.json")


def test_reconcile_sent_delivery_repairs_subscription_without_resend(monkeypatch, tmp_path):
    isolate_subscription_file(monkeypatch, tmp_path)
    today = datetime.now().astimezone().date().isoformat()
    key = auto_summary_key("space1", "today", today)
    subscription.enable_summary_subscription("space1", "chat1")
    reserve_delivery(key, delivery_type="auto_summary", space_id="space1")
    mark_sent(key)

    called = []

    class FakeExecutor:
        def submit_summary(self, *args, **kwargs):
            called.append((args, kwargs))
            raise AssertionError("should not submit summary when delivery is already sent")

    count = scheduler.run_summary_scheduler_once(lambda chat_id, text: True, executor=FakeExecutor())

    assert count == 0
    assert called == []
    assert subscription.get_summary_subscription("space1").last_sent_date == today


def test_sent_delivery_with_failed_subscription_update_is_repaired_next_tick(monkeypatch, tmp_path):
    isolate_subscription_file(monkeypatch, tmp_path)
    today = datetime.now().astimezone().date().isoformat()
    key = auto_summary_key("space1", "today", today)
    subscription.enable_summary_subscription("space1", "chat1")
    reserve_delivery(key, delivery_type="auto_summary", space_id="space1")
    mark_sent(key)

    assert reconciliation.reconcile_auto_summary_delivery("space1", "today", today) is True
    assert subscription.get_summary_subscription("space1").last_sent_date == today


def test_unknown_delivery_skips_auto_summary(monkeypatch, tmp_path):
    isolate_subscription_file(monkeypatch, tmp_path)
    today = datetime.now().astimezone().date().isoformat()
    key = auto_summary_key("space1", "today", today)
    subscription.enable_summary_subscription("space1", "chat1")
    reserve_delivery(key, delivery_type="auto_summary", space_id="space1")
    mark_unknown(key, "timeout")

    called = []

    class FakeExecutor:
        def submit_summary(self, *args, **kwargs):
            called.append((args, kwargs))
            raise AssertionError("unknown delivery should not be resent automatically")

    assert scheduler.run_summary_scheduler_once(lambda chat_id, text: True, executor=FakeExecutor()) == 0
    assert called == []
    assert subscription.get_summary_subscription("space1").last_sent_date is None


def test_failed_delivery_allows_scheduler_to_submit_again(monkeypatch, tmp_path):
    isolate_subscription_file(monkeypatch, tmp_path)
    today = FIXED_NOW.date().isoformat()
    key = auto_summary_key("space1", "today", today)
    subscription.enable_summary_subscription("space1", "chat1")
    subscription.update_summary_time("space1", "chat1", "00:00")
    reserve_delivery(key, delivery_type="auto_summary", space_id="space1")
    mark_failed(key, "send failed")
    submitted = []

    class FakeExecutor:
        def submit_summary(self, space_id, range_key, chat_id, message_id=None, on_success=None, delivery_key=None, delivery_type=None):
            submitted.append((space_id, range_key, chat_id, delivery_key, delivery_type))
            return create_task("summary", space_id, {})

    assert scheduler.run_summary_scheduler_once(lambda chat_id, text: True, executor=FakeExecutor(), now=FIXED_NOW) == 1
    assert submitted == [("space1", "today", "chat1", key, "auto_summary")]


def test_expired_reserved_auto_summary_can_be_submitted_again(monkeypatch, tmp_path):
    isolate_subscription_file(monkeypatch, tmp_path)
    today = FIXED_NOW.date().isoformat()
    key = auto_summary_key("space1", "today", today)
    subscription.enable_summary_subscription("space1", "chat1")
    subscription.update_summary_time("space1", "chat1", "00:00")
    reserve_delivery(key, delivery_type="auto_summary", space_id="space1")
    _patch_delivery(
        tmp_path,
        key,
        lease_expires_at=(datetime.now().astimezone() - timedelta(minutes=1)).isoformat(),
    )
    submitted = []

    class FakeExecutor:
        def submit_summary(self, space_id, range_key, chat_id, message_id=None, on_success=None, delivery_key=None, delivery_type=None):
            submitted.append((space_id, range_key, chat_id, delivery_key, delivery_type))
            return create_task("summary", space_id, {})

    assert scheduler.run_summary_scheduler_once(lambda chat_id, text: True, executor=FakeExecutor(), now=FIXED_NOW) == 1
    assert submitted == [("space1", "today", "chat1", key, "auto_summary")]


def _patch_delivery(tmp_path, key, **updates):
    path = tmp_path / "deliveries" / "index.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw[key].update(updates)
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
