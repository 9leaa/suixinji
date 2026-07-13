from runtime import delivery_store
from runtime.delivery_store import (
    DELIVERY_FAILED,
    DELIVERY_SENT,
    DELIVERY_UNKNOWN,
    mark_failed,
    mark_sent,
    mark_unknown,
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
