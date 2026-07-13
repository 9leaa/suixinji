import json

import pytest

from summary import subscription


def isolate_subscription_file(monkeypatch, tmp_path):
    monkeypatch.setattr(subscription, "DATA_DIR", tmp_path)
    monkeypatch.setattr(subscription, "SUBSCRIPTIONS_PATH", tmp_path / "summary_subscriptions.json")


def test_parse_summary_time_accepts_hh_mm_only():
    assert subscription.parse_summary_time("22:00") == "22:00"
    assert subscription.parse_summary_time("00:05") == "00:05"
    assert subscription.parse_summary_time("24:00") is None
    assert subscription.parse_summary_time("7:00") is None
    assert subscription.parse_summary_time("22:60") is None


def test_enable_disable_and_status(monkeypatch, tmp_path):
    isolate_subscription_file(monkeypatch, tmp_path)

    sub = subscription.enable_summary_subscription("space1", "chat1")
    assert sub.enabled is True
    assert sub.time == "22:00"
    assert sub.range_key == "today"

    loaded = subscription.get_summary_subscription("space1")
    assert loaded == sub

    disabled = subscription.disable_summary_subscription("space1")
    assert disabled is not None
    assert disabled.enabled is False
    assert subscription.list_enabled_summary_subscriptions() == []


def test_update_summary_time_preserves_existing_state(monkeypatch, tmp_path):
    isolate_subscription_file(monkeypatch, tmp_path)
    subscription.enable_summary_subscription("space1", "chat1")
    subscription.mark_summary_sent("space1", "2026-06-07")

    sub = subscription.update_summary_time("space1", "chat2", "21:30")

    assert sub.chat_id == "chat2"
    assert sub.time == "21:30"
    assert sub.last_sent_date == "2026-06-07"

    raw = json.loads((tmp_path / "summary_subscriptions.json").read_text(encoding="utf-8"))
    assert raw["space1"]["time"] == "21:30"


def test_update_summary_time_rejects_invalid_time(monkeypatch, tmp_path):
    isolate_subscription_file(monkeypatch, tmp_path)

    with pytest.raises(ValueError):
        subscription.update_summary_time("space1", "chat1", "25:00")


def test_mark_summary_sent_is_noop_for_missing_subscription(monkeypatch, tmp_path):
    isolate_subscription_file(monkeypatch, tmp_path)

    subscription.mark_summary_sent("missing", "2026-06-07")

    assert not (tmp_path / "summary_subscriptions.json").exists()
