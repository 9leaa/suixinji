from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from eval.layer3.contract_migrations.v1_to_v2 import migrate_case
from eval.layer3.run_layer3_eval import (
    DATA_FILES,
    Layer3PreflightError,
    _candidate_for,
    _fixture_version_task_status,
    load_cases_with_manifest,
)
from memory.models import MemoryCandidate


def _v1_case(case_id: str) -> dict:
    return {
        "schema_version": "suixinji.layer3.retrieval_answer.v1",
        "case_id": case_id,
        "dataset": "current_state_retrieval",
        "coverage_tags": [],
        "input": {
            "memory_snapshot": {
                "memories": [{
                    "memory_ref": "m1", "memory_type": "task", "content": "修复评测，正在等待权限",
                    "task_status": "blocked", "source_refs": ["s1"], "updated_at": "2026-08-01T00:00:00Z",
                }],
                "versions": [{
                    "version_ref": "v1", "memory_ref": "m1", "sequence": 1, "content": "修复评测，正在等待权限",
                    "task_status": "blocked", "valid_from": "2026-08-01T00:00:00Z", "source_refs": ["s1"],
                }],
                "sources": [{"source_ref": "s1"}],
            },
        },
        "expected": {
            "answer_type": "current_state", "relevant_current_refs": ["m1"], "relevant_history_refs": [],
            "expected_claims": [{"claim": "修复评测", "memory_refs": ["m1"], "source_refs": ["s1"]}],
            "required_citation_refs": ["s1"], "no_answer": False,
        },
    }


def _write_cases(directory: Path, cases: list[dict]) -> None:
    for index, name in enumerate(DATA_FILES):
        payload = copy.deepcopy(cases[index % len(cases)])
        payload["case_id"] = f"{payload['case_id']}-{index}"
        payload["dataset"] = name.removesuffix(".jsonl")
        (directory / name).write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def test_v1_zip_preflight_migrates_all_cases_without_source_mutation():
    source = Path("eval/packages/suixinji_layer3_datasets_v1.zip")
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    cases, manifest = load_cases_with_manifest(str(source))

    assert len(cases) == 520
    assert manifest["source_contract"] == "suixinji.layer3.retrieval_answer.v1"
    assert manifest["effective_contract"] == "suixinji.layer3.retrieval_answer.v2"
    assert manifest["contract_migration_applied"] is True
    assert manifest["source_data_sha256"] == before
    assert manifest["preflight"]["case_count"] == 520
    assert manifest["preflight"]["v2_contract_valid"] is True
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_schema_mixture_and_unknown_schema_fail_before_execution(tmp_path: Path):
    v1 = _v1_case("v1")
    v2, _ = migrate_case(v1)
    _write_cases(tmp_path, [v1, v2])
    with pytest.raises(Layer3PreflightError, match="mixed Layer3 schemas"):
        load_cases_with_manifest(str(tmp_path))

    unknown_dir = tmp_path / "unknown"
    unknown_dir.mkdir()
    unknown = _v1_case("unknown")
    unknown["schema_version"] = "suixinji.layer3.retrieval_answer.v99"
    _write_cases(unknown_dir, [unknown])
    with pytest.raises(Layer3PreflightError, match="unsupported Layer3 schema"):
        load_cases_with_manifest(str(unknown_dir))


@pytest.mark.parametrize(
    ("legacy", "expected", "scope_key"),
    [
        ("blocked", "todo", "legacy_task_status"),
        ("in_progress", "todo", "legacy_task_status"),
        ("cancelled", "done", "legacy_task_status"),
        ("todo", "todo", None),
        ("done", "done", None),
    ],
)
def test_fixture_task_adapter_projects_status_without_mutating_fixture(legacy, expected, scope_key):
    raw = {
        "memory_type": "task", "content": "修复评测，正在等待权限", "task_status": legacy,
        "updated_at": "2026-08-01T00:00:00Z", "source_refs": ["s1"],
    }
    before = copy.deepcopy(raw)

    candidate = _candidate_for(raw, "layer3:case:s1")
    version_status = _fixture_version_task_status(raw, "task")

    assert candidate.task_status == expected
    assert version_status == expected
    assert candidate.content == raw["content"]
    assert candidate.valid_from == "2026-08-01T00:00:00+00:00"
    assert candidate.note_id == "layer3:case:s1"
    assert (scope_key in candidate.scope) is (scope_key is not None)
    assert raw == before


def test_fixture_adapter_strips_non_task_status_and_production_invariant_remains_strict():
    candidate = _candidate_for(
        {"memory_type": "semantic", "content": "用户住在上海", "task_status": "blocked"}, "layer3:case:s1"
    )
    assert candidate.task_status is None
    assert _fixture_version_task_status({"task_status": "blocked"}, "semantic") is None
    with pytest.raises(ValueError, match="invalid task_status: blocked"):
        MemoryCandidate("task", "修复评测", 0.8, 0.9, task_status="blocked")
