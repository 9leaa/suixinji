from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.external.longmemeval import run_longmemeval_s_retrieval_v2 as runner
from eval.external.longmemeval.run_longmemeval_s_retrieval import _note_id


def test_note_id_is_namespaced_by_disposable_space() -> None:
    first = _note_id("case", "session", 0, namespace="space-a")
    second = _note_id("case", "session", 0, namespace="space-b")

    assert first != second
    assert first == _note_id("case", "session", 0, namespace="space-a")


def test_checkpoint_loader_keeps_latest_case_result(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.jsonl"
    checkpoint.write_text(
        "\n".join([
            json.dumps({"question_id": "a", "error": "temporary"}),
            "not-json",
            json.dumps({"question_id": "a", "error": None, "mrr": 1.0}),
            json.dumps({"question_id": "b", "error": None, "mrr": 0.5}),
        ]) + "\n",
        encoding="utf-8",
    )

    rows, invalid_lines = runner._load_checkpoint(checkpoint)

    assert invalid_lines == 1
    assert rows["a"]["error"] is None
    assert rows["a"]["mrr"] == 1.0
    assert rows["b"]["mrr"] == 0.5


def test_case_retry_returns_only_recovered_result(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}
    sleeps: list[float] = []

    def operation() -> dict[str, object]:
        calls["count"] += 1
        if calls["count"] < 3:
            return {"question_id": "case", "error": "connection refused"}
        return {"question_id": "case", "error": None, "mrr": 1.0}

    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    result = runner._run_with_retry(operation, attempts=4, base_seconds=0.25, max_seconds=1.0)

    assert calls["count"] == 3
    assert result["error"] is None
    assert result["attempt_count"] == 3
    assert result["recovered_after_retry"] is True
    assert sleeps == [0.25, 0.5]


def test_case_retry_preserves_final_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    def operation() -> dict[str, object]:
        calls["count"] += 1
        return {"question_id": "case", "error": "still unavailable"}

    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    result = runner._run_with_retry(operation, attempts=2, base_seconds=0.0, max_seconds=0.0)

    assert calls["count"] == 2
    assert result["error"] == "still unavailable"
    assert result["attempt_count"] == 2
    assert result["recovered_after_retry"] is False
