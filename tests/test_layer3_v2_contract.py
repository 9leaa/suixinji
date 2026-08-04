from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from eval.layer3.contract_migrations.v1_to_v2 import migrate_case, materialize_v2
from eval.layer3.contracts.v2 import ContractValidationError, SCHEMA_VERSION, validate_case


def _case(*, tags: list[str], answer_type: str = "history") -> dict:
    return {
        "schema_version": "suixinji.layer3.retrieval_answer.v1",
        "case_id": "case-1",
        "dataset": "history_and_temporal",
        "coverage_tags": tags,
        "input": {"memory_snapshot": {"versions": [
            {"version_ref": "v2", "sequence": 2, "task_status": "blocked", "source_refs": ["s2"]},
            {"version_ref": "v1", "sequence": 1, "task_status": "todo", "source_refs": ["s1"]},
        ]}},
        "expected": {
            "answer_type": answer_type,
            "relevant_current_refs": ["m1"],
            "relevant_history_refs": ["v1", "v2"],
            "expected_claims": [{"claim": "任务依次经历todo、blocked", "memory_refs": ["m1"], "version_refs": ["v1", "v2"], "source_refs": ["s1", "s2"]}],
            "required_citation_refs": ["s1", "s2"],
            "no_answer": False,
        },
    }


def test_v2_migration_keeps_v1_object_unchanged_and_adds_timeline_group():
    source = _case(tags=["history", "task_transition"])
    before = copy.deepcopy(source)
    migrated, entry = migrate_case(source)

    assert source == before
    assert migrated["schema_version"] == SCHEMA_VERSION
    assert migrated["expected"]["answer_type"] == "answered"
    assert migrated["expected"]["evidence_mode"] == "history"
    group = migrated["expected"]["expected_claim_groups"][0]
    assert [item["version_ref"] for item in group["ordered_members"]] == ["v1", "v2"]
    assert entry["claim_granularity_upgrade"] is True
    validate_case(migrated)


@pytest.mark.parametrize(
    ("tags", "old_type", "new_type", "mode"),
    [
        (["sensitive", "access_control"], "no_answer", "restricted", "none"),
        (["conflict"], "qualified", "conflict", "mixed"),
        (["stale_only"], "qualified", "qualified_history_only", "history"),
    ],
)
def test_v2_answer_type_contract_migration(tags, old_type, new_type, mode):
    source = _case(tags=tags, answer_type=old_type)
    migrated, _ = migrate_case(source)
    assert migrated["expected"]["answer_type"] == new_type
    assert migrated["expected"]["evidence_mode"] == mode
    if new_type in {"restricted", "conflict"}:
        assert migrated["expected"]["expected_claims"] == []


def test_v2_rejects_non_fact_claims():
    case, _ = migrate_case(_case(tags=["sensitive"], answer_type="no_answer"))
    case["expected"]["expected_claims"] = [{"claim": "sensitive value"}]
    with pytest.raises(ContractValidationError, match="must not require"):
        validate_case(case)


def test_materialize_v2_does_not_modify_source_files(tmp_path: Path):
    source = tmp_path / "v1"
    source.mkdir()
    payload = _case(tags=["history"])
    names = (
        "current_state_retrieval.jsonl", "history_and_temporal.jsonl", "multi_memory_answer_and_citation.jsonl",
        "no_answer_conflict_and_stale.jsonl", "semantic_paraphrase_and_noise.jsonl",
    )
    for name in names:
        case = {**payload, "case_id": f"case-{name}", "dataset": name.removesuffix(".jsonl")}
        (source / name).write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")
    hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in names}

    result = materialize_v2(source, tmp_path / "v2")

    assert result["case_count"] == 5
    assert hashes == {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in names}
    assert (tmp_path / "v2" / "contract_change_manifest.json").exists()
