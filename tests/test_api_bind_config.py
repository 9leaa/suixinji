"""文件作用：API host/bind 与配置安全约束。

项目关系：本文件依赖 `scripts.load_test_multi_users`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from scripts.load_test_multi_users import default_endpoint


ROOT = Path(__file__).resolve().parents[1]


def _settings_import(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """函数功能：`_settings_import` 负责处理 settings import，服务于本文件职责：API host/bind 与配置安全约束。
    传参：
        env: env 参数，由调用方传入，类型为 `dict[str, str]`。
    返回结果说明：
        返回 `subprocess.CompletedProcess[str]` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    merged = os.environ.copy()
    merged.update(
        {
            "PYTHONPATH": str(ROOT),
            "STORAGE_BACKEND": "local",
            "COORDINATION_BACKEND": "local",
            "TASK_QUEUE_BACKEND": "local",
        }
    )
    merged.update(env)
    return subprocess.run(
        [sys.executable, "-c", "import core.settings as s; print(s.API_HOST, s.API_PORT)"],
        cwd=ROOT,
        env=merged,
        capture_output=True,
        text=True,
        check=False,
    )


def test_api_bind_defaults_and_overrides() -> None:
    """函数功能：`test_api_bind_defaults_and_overrides` 负责验证 api bind defaults and overrides 场景，服务于本文件职责：API host/bind 与配置安全约束。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    result = _settings_import({"SUIXINJI_API_HOST": "127.0.0.1", "SUIXINJI_API_PORT": "8000"})
    assert result.returncode == 0
    assert "127.0.0.1 8000" in result.stdout

    result = _settings_import({"SUIXINJI_API_HOST": "0.0.0.0", "SUIXINJI_API_PORT": "18000"})
    assert result.returncode == 0
    assert "0.0.0.0 18000" in result.stdout


def test_api_bind_rejects_invalid_port_and_host() -> None:
    """函数功能：`test_api_bind_rejects_invalid_port_and_host` 负责验证 api bind rejects invalid port and host 场景，服务于本文件职责：API host/bind 与配置安全约束。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    assert _settings_import({"SUIXINJI_API_HOST": "127.0.0.1", "SUIXINJI_API_PORT": "70000"}).returncode != 0
    assert _settings_import({"SUIXINJI_API_HOST": "bad host", "SUIXINJI_API_PORT": "8000"}).returncode != 0


def test_load_test_default_endpoint_uses_api_bind_env(monkeypatch) -> None:
    """函数功能：`test_load_test_default_endpoint_uses_api_bind_env` 负责验证 load test default endpoint uses api bind env 场景，服务于本文件职责：API host/bind 与配置安全约束。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setenv("SUIXINJI_API_HOST", "127.0.0.9")
    monkeypatch.setenv("SUIXINJI_API_PORT", "18000")

    assert default_endpoint() == "http://127.0.0.9:18000"
