from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from scripts.load_test_multi_users import default_endpoint


ROOT = Path(__file__).resolve().parents[1]


def _settings_import(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """验证“settingsimport”场景的预期行为与回归边界。"""
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
    """验证“APIbinddefaultsandoverrides”场景的预期行为与回归边界。"""
    result = _settings_import({"SUIXINJI_API_HOST": "127.0.0.1", "SUIXINJI_API_PORT": "8000"})
    assert result.returncode == 0
    assert "127.0.0.1 8000" in result.stdout

    result = _settings_import({"SUIXINJI_API_HOST": "0.0.0.0", "SUIXINJI_API_PORT": "18000"})
    assert result.returncode == 0
    assert "0.0.0.0 18000" in result.stdout


def test_api_bind_rejects_invalid_port_and_host() -> None:
    """验证“APIbindrejectsinvalidportandhost”场景的预期行为与回归边界。"""
    assert _settings_import({"SUIXINJI_API_HOST": "127.0.0.1", "SUIXINJI_API_PORT": "70000"}).returncode != 0
    assert _settings_import({"SUIXINJI_API_HOST": "bad host", "SUIXINJI_API_PORT": "8000"}).returncode != 0


def test_load_test_default_endpoint_uses_api_bind_env(monkeypatch) -> None:
    """验证“加载默认endpointusesAPIbindenv”场景的预期行为与回归边界。"""
    monkeypatch.setenv("SUIXINJI_API_HOST", "127.0.0.9")
    monkeypatch.setenv("SUIXINJI_API_PORT", "18000")

    assert default_endpoint() == "http://127.0.0.9:18000"
