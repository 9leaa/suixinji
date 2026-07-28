from datetime import datetime, timezone

import pytest

from runtime.delivery_store import auto_summary_key, mark_sent, reserve_delivery
from runtime.task import create_task
from summary import reconciliation, scheduler, subscription

FIXED_NOW = datetime(2026, 7, 14, 23, 0, tzinfo=timezone.utc)


def isolate_subscription_file(monkeypatch, tmp_path):
    """验证“isolatesubscriptionfile”场景的预期行为与回归边界。"""
    monkeypatch.setattr(subscription, "DATA_DIR", tmp_path)
    monkeypatch.setattr(subscription, "SUBSCRIPTIONS_PATH", tmp_path / "summary_subscriptions.json")


def test_reconcile_failure_skips_subscription_without_submitting(monkeypatch, tmp_path):
    """验证“reconcilefailureskipssubscriptionwithoutsubmitting”场景的预期行为与回归边界。"""
    isolate_subscription_file(monkeypatch, tmp_path)
    today = datetime.now().astimezone().date().isoformat()
    key = auto_summary_key("space1", "today", today)
    subscription.enable_summary_subscription("space1", "chat1")
    reserve_delivery(key, delivery_type="auto_summary", space_id="space1")
    mark_sent(key)
    monkeypatch.setattr(reconciliation, "mark_summary_sent", lambda space_id, day: (_ for _ in ()).throw(RuntimeError("write failed")))
    submitted = []

    class FakeExecutor:
        def submit_summary(self, *args, **kwargs):
            """验证“submit总结”场景的预期行为与回归边界。"""
            submitted.append((args, kwargs))
            raise AssertionError("should not submit when sent delivery reconciliation fails")

    assert scheduler.run_summary_scheduler_once(lambda chat_id, text: True, executor=FakeExecutor()) == 0
    assert submitted == []
    assert subscription.get_summary_subscription("space1").last_sent_date is None


def test_next_tick_repairs_subscription_after_previous_reconcile_failure(monkeypatch, tmp_path):
    """验证“下一步tickrepairssubscription后置previousreconcilefailure”场景的预期行为与回归边界。"""
    isolate_subscription_file(monkeypatch, tmp_path)
    today = datetime.now().astimezone().date().isoformat()
    key = auto_summary_key("space1", "today", today)
    subscription.enable_summary_subscription("space1", "chat1")
    reserve_delivery(key, delivery_type="auto_summary", space_id="space1")
    mark_sent(key)
    calls = {"count": 0}

    def flaky_mark(space_id, day):
        """验证“flaky标记”场景的预期行为与回归边界。"""
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("write failed")
        subscription.mark_summary_sent(space_id, day)

    monkeypatch.setattr(reconciliation, "mark_summary_sent", flaky_mark)

    class FakeExecutor:
        def submit_summary(self, *args, **kwargs):
            """验证“submit总结”场景的预期行为与回归边界。"""
            raise AssertionError("sent delivery reconciliation should not submit summary")

    assert scheduler.run_summary_scheduler_once(lambda chat_id, text: True, executor=FakeExecutor()) == 0
    assert subscription.get_summary_subscription("space1").last_sent_date is None
    assert scheduler.run_summary_scheduler_once(lambda chat_id, text: True, executor=FakeExecutor()) == 0
    assert subscription.get_summary_subscription("space1").last_sent_date == today
    assert calls["count"] == 2


def test_one_subscription_reconcile_failure_does_not_block_other_subscription(monkeypatch, tmp_path):
    """验证“onesubscriptionreconcilefailuredoesnotblockothersubscription”场景的预期行为与回归边界。"""
    isolate_subscription_file(monkeypatch, tmp_path)
    today = FIXED_NOW.date().isoformat()
    key = auto_summary_key("space_a", "today", today)
    subscription.enable_summary_subscription("space_a", "chat_a")
    subscription.enable_summary_subscription("space_b", "chat_b")
    subscription.update_summary_time("space_a", "chat_a", "00:00")
    subscription.update_summary_time("space_b", "chat_b", "00:00")
    reserve_delivery(key, delivery_type="auto_summary", space_id="space_a")
    mark_sent(key)

    def fail_for_space_a(space_id, day):
        """验证“失败for空间a”场景的预期行为与回归边界。"""
        if space_id == "space_a":
            raise RuntimeError("write failed")
        subscription.mark_summary_sent(space_id, day)

    monkeypatch.setattr(reconciliation, "mark_summary_sent", fail_for_space_a)
    submitted = []

    class FakeExecutor:
        def submit_summary(self, space_id, range_key, chat_id, message_id=None, on_success=None, delivery_key=None, delivery_type=None):
            """验证“submit总结”场景的预期行为与回归边界。"""
            submitted.append((space_id, range_key, chat_id, delivery_key, delivery_type))
            return create_task("summary", space_id, {})

    assert scheduler.run_summary_scheduler_once(lambda chat_id, text: True, executor=FakeExecutor(), now=FIXED_NOW) == 1
    assert submitted == [("space_b", "today", "chat_b", auto_summary_key("space_b", "today", today), "auto_summary")]


def test_run_scheduler_tick_safely_catches_tick_failure(monkeypatch):
    """验证“运行schedulerticksafelycatchestickfailure”场景的预期行为与回归边界。"""
    calls = {"count": 0}

    def fake_run_once(send_text, executor=None):
        """验证“fake运行once”场景的预期行为与回归边界。"""
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("tick failed")

    monkeypatch.setattr(scheduler, "run_summary_scheduler_once", fake_run_once)

    scheduler.run_scheduler_tick_safely(lambda chat_id, text: True)
    scheduler.run_scheduler_tick_safely(lambda chat_id, text: True)

    assert calls["count"] == 2


def test_run_scheduler_tick_safely_survives_failure_logging_error(monkeypatch):
    """验证“运行schedulerticksafelysurvivesfailurelogging错误”场景的预期行为与回归边界。"""
    monkeypatch.setattr(scheduler, "run_summary_scheduler_once", lambda send_text, executor=None: (_ for _ in ()).throw(RuntimeError("tick failed")))
    monkeypatch.setattr(scheduler, "log_event", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("log failed")))

    try:
        scheduler.run_scheduler_tick_safely(lambda chat_id, text: True)
    except Exception as exc:
        pytest.fail(f"safe tick should not raise, got {exc!r}")
