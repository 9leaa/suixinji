import os

os.environ["STORAGE_BACKEND"] = "local"
os.environ["COORDINATION_BACKEND"] = "local"
os.environ["TASK_QUEUE_BACKEND"] = "local"
os.environ["SUIXINJI_AGENT_HOOKS_ENABLED"] = "false"

import pytest

from runtime import delivery_store
from memory import repository as memory_repository
from memory import trace as memory_trace


@pytest.fixture(autouse=True)
def isolate_delivery_store(monkeypatch, tmp_path):
    monkeypatch.setattr(delivery_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(delivery_store, "DELIVERY_DIR", tmp_path / "deliveries")
    monkeypatch.setattr(delivery_store, "DELIVERY_PATH", tmp_path / "deliveries" / "index.json")


@pytest.fixture(autouse=True)
def isolate_memory_store(monkeypatch, tmp_path):
    monkeypatch.setattr(memory_repository, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(memory_trace, "TRACE_PATH", tmp_path / "traces.jsonl")
