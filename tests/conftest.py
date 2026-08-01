"""文件作用：pytest fixture、临时存储和环境隔离。

项目关系：本文件依赖 `memory`、`runtime`；被 暂无静态导入方或仅作为入口脚本执行。
"""


import os

os.environ["STORAGE_BACKEND"] = "local"
os.environ["COORDINATION_BACKEND"] = "local"
os.environ["TASK_QUEUE_BACKEND"] = "local"
os.environ["SUIXINJI_AGENT_HOOKS_ENABLED"] = "false"
# 即使开发者本地 .env 启用了 LLM，也保持测试确定性，并避免正在运行的飞书服务触发真实抽取。
os.environ["SUIXINJI_MEMORY_EXTRACTOR_MODE"] = "rules"

import pytest

from runtime import delivery_store
from memory import repository as memory_repository
from memory import trace as memory_trace


@pytest.fixture(autouse=True)
def isolate_delivery_store(monkeypatch, tmp_path):
    """函数功能：`isolate_delivery_store` 负责处理 isolate delivery store，服务于本文件职责：pytest fixture、临时存储和环境隔离。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
        tmp_path: tmp path 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(delivery_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(delivery_store, "DELIVERY_DIR", tmp_path / "deliveries")
    monkeypatch.setattr(delivery_store, "DELIVERY_PATH", tmp_path / "deliveries" / "index.json")


@pytest.fixture(autouse=True)
def isolate_memory_store(monkeypatch, tmp_path):
    """函数功能：`isolate_memory_store` 负责处理 isolate memory store，服务于本文件职责：pytest fixture、临时存储和环境隔离。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
        tmp_path: tmp path 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(memory_repository, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(memory_trace, "TRACE_PATH", tmp_path / "traces.jsonl")
