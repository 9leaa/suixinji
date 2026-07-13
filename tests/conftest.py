import pytest

from runtime import delivery_store


@pytest.fixture(autouse=True)
def isolate_delivery_store(monkeypatch, tmp_path):
    monkeypatch.setattr(delivery_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(delivery_store, "DELIVERY_DIR", tmp_path / "deliveries")
    monkeypatch.setattr(delivery_store, "DELIVERY_PATH", tmp_path / "deliveries" / "index.json")
