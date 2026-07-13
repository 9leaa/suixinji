import json
from datetime import datetime, timedelta

from runtime import delivery_store
from runtime.delivery_store import (
    DELIVERY_FAILED,
    DELIVERY_RESERVED,
    DELIVERY_SENT,
    DELIVERY_UNKNOWN,
    mark_failed,
    mark_sent,
    mark_unknown,
    recover_stale_reserved_deliveries,
    reserve_delivery,
)


def isolate_delivery_store(monkeypatch, tmp_path):
    monkeypatch.setattr(delivery_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(delivery_store, "DELIVERY_DIR", tmp_path / "deliveries")
    monkeypatch.setattr(delivery_store, "DELIVERY_PATH", tmp_path / "deliveries" / "index.json")


def test_delivery_key_can_only_be_reserved_once_until_terminal_state(monkeypatch, tmp_path):
    isolate_delivery_store(monkeypatch, tmp_path)

    first = reserve_delivery("query:s:m1", delivery_type="query", space_id="s", message_id="m1")
    second = reserve_delivery("query:s:m1", delivery_type="query", space_id="s", message_id="m1")

    assert first is not None
    assert second is None


def test_sent_and_unknown_are_not_reserved_again(monkeypatch, tmp_path):
    isolate_delivery_store(monkeypatch, tmp_path)

    assert reserve_delivery("k1", delivery_type="query", space_id="s") is not None
    mark_sent("k1")
    assert reserve_delivery("k1", delivery_type="query", space_id="s") is None
    assert delivery_store.get_delivery("k1").status == DELIVERY_SENT

    assert reserve_delivery("k2", delivery_type="query", space_id="s") is not None
    mark_unknown("k2", "TimeoutError")
    assert reserve_delivery("k2", delivery_type="query", space_id="s") is None
    assert delivery_store.get_delivery("k2").status == DELIVERY_UNKNOWN


def test_failed_delivery_can_be_reserved_again(monkeypatch, tmp_path):
    isolate_delivery_store(monkeypatch, tmp_path)

    assert reserve_delivery("k1", delivery_type="query", space_id="s") is not None
    mark_failed("k1", "send failed")

    assert delivery_store.get_delivery("k1").status == DELIVERY_FAILED
    assert reserve_delivery("k1", delivery_type="query", space_id="s") is not None


def test_expired_reserved_delivery_can_be_reserved_again(monkeypatch, tmp_path):
    isolate_delivery_store(monkeypatch, tmp_path)

    first = reserve_delivery("k1", delivery_type="query", space_id="s")
    assert first is not None
    _patch_delivery(
        tmp_path,
        "k1",
        lease_expires_at=(datetime.now().astimezone() - timedelta(minutes=10)).isoformat(),
    )

    second = reserve_delivery("k1", delivery_type="query", space_id="s")

    assert second is not None
    assert second.status == DELIVERY_RESERVED
    assert second.attempt_count == first.attempt_count + 1
    assert datetime.fromisoformat(second.lease_expires_at) > datetime.now().astimezone()


def test_recover_stale_reserved_deliveries_marks_expired_reserved_as_failed(monkeypatch, tmp_path):
    isolate_delivery_store(monkeypatch, tmp_path)

    reserve_delivery("k1", delivery_type="query", space_id="s")
    _patch_delivery(
        tmp_path,
        "k1",
        reserved_at=(datetime.now().astimezone() - timedelta(minutes=20)).isoformat(),
        lease_expires_at=(datetime.now().astimezone() - timedelta(minutes=10)).isoformat(),
    )

    assert recover_stale_reserved_deliveries() == 1
    assert delivery_store.get_delivery("k1").status == DELIVERY_FAILED
    assert reserve_delivery("k1", delivery_type="query", space_id="s") is not None


def test_delivery_stops_after_max_attempts(monkeypatch, tmp_path):
    isolate_delivery_store(monkeypatch, tmp_path)
    monkeypatch.setattr(delivery_store, "DELIVERY_MAX_ATTEMPTS", 3)

    for _index in range(3):
        record = reserve_delivery("k1", delivery_type="query", space_id="s")
        assert record is not None
        mark_failed("k1", "failed")

    assert delivery_store.get_delivery("k1").attempt_count == 3
    assert reserve_delivery("k1", delivery_type="query", space_id="s") is None


def _patch_delivery(tmp_path, key, **updates):
    path = tmp_path / "deliveries" / "index.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw[key].update(updates)
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
