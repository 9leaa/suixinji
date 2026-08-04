"""Read-only migration from Layer 3 v1 cases to the v2 evaluator contract."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

from eval.layer3.contracts.v2 import SCHEMA_VERSION, validate_cases

DATA_FILES = (
    "current_state_retrieval.jsonl",
    "history_and_temporal.jsonl",
    "multi_memory_answer_and_citation.jsonl",
    "no_answer_conflict_and_stale.jsonl",
    "semantic_paraphrase_and_noise.jsonl",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _answer_contract(case: dict[str, Any]) -> tuple[str, str, str, str]:
    expected = case["expected"]
    tags = set(case.get("coverage_tags") or [])
    old_type = str(expected.get("answer_type") or "")
    if "sensitive" in tags or "access_control" in tags:
        return "restricted", "none", "acl_filtered_all_evidence", "security_contract_upgrade"
    if "conflict" in tags:
        return "conflict", "mixed", "pending_review_conflict", "pending_review_contract_upgrade"
    if old_type == "clarification":
        return "clarification", "none", "ambiguous_reference", "answer_type_normalization"
    if old_type == "qualified":
        return "qualified_history_only", "history", "stale_history_only", "answer_type_normalization"
    if old_type == "no_answer" or bool(expected.get("no_answer")):
        return "no_answer", "none", "no_relevant_evidence", "answer_type_normalization"
    if expected.get("relevant_history_refs"):
        return "answered", "history", "history_query", "answer_type_normalization"
    return "answered", "current", "evidence_supported", "answer_type_normalization"


def _timeline_group(case: dict[str, Any]) -> dict[str, Any] | None:
    expected = case["expected"]
    version_refs = list(expected.get("relevant_history_refs") or [])
    if len(version_refs) < 2:
        return None
    snapshot = (case.get("input") or {}).get("memory_snapshot") or {}
    by_ref = {str(item.get("version_ref")): item for item in snapshot.get("versions") or []}
    ordered = sorted(
        (by_ref.get(ref, {"version_ref": ref}) for ref in version_refs),
        key=lambda item: (int(item.get("sequence") or 0), str(item.get("version_ref") or "")),
    )
    primary = (expected.get("expected_claims") or [{}])[0]
    return {
        "group_type": "timeline",
        "summary_claim": copy.deepcopy(primary),
        "ordered_members": [
            {
                "version_ref": str(item.get("version_ref")),
                "sequence": item.get("sequence"),
                "value": item.get("value") or item.get("task_status"),
                "source_refs": list(item.get("source_refs") or []),
            }
            for item in ordered
        ],
        "memory_refs": list(primary.get("memory_refs") or []),
        "version_refs": [str(item.get("version_ref")) for item in ordered],
        "source_refs": list(primary.get("source_refs") or []),
        "support_role": "history",
    }


def migrate_case(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create a v2 case and complete per-case change record without mutating v1."""
    migrated = copy.deepcopy(case)
    old_schema = migrated.get("schema_version")
    old_expected = copy.deepcopy(migrated["expected"])
    new_type, mode, reason, type_reason = _answer_contract(migrated)
    expected = migrated["expected"]
    expected["answer_type"] = new_type
    expected["evidence_mode"] = mode
    expected["reason_code"] = reason
    if new_type in {"no_answer", "restricted", "clarification", "conflict"}:
        expected["expected_claims"] = []
        expected["required_citation_refs"] = []
    group = _timeline_group(migrated)
    if group is not None:
        expected["expected_claim_groups"] = [group]
    else:
        expected["expected_claim_groups"] = []
    migrated["schema_version"] = SCHEMA_VERSION
    changes = [
        {
            "field": "schema_version",
            "v1": old_schema,
            "v2": SCHEMA_VERSION,
            "reason": "versioned_contract_introduction",
        },
        {
            "field": "expected.answer_type/evidence_mode/reason_code",
            "v1": {"answer_type": old_expected.get("answer_type")},
            "v2": {"answer_type": new_type, "evidence_mode": mode, "reason_code": reason},
            "reason": type_reason,
        },
    ]
    if group is not None:
        changes.append({
            "field": "expected.expected_claim_groups",
            "v1": None,
            "v2": {"group_type": "timeline", "version_refs": group["version_refs"]},
            "reason": "claim_granularity_upgrade",
        })
    if new_type in {"restricted", "conflict"}:
        changes.append({
            "field": "expected.expected_claims/required_citation_refs",
            "v1": {"expected_claims": old_expected.get("expected_claims"), "required_citation_refs": old_expected.get("required_citation_refs")},
            "v2": {"expected_claims": [], "required_citation_refs": []},
            "reason": "non_fact_response_has_no_sensitive_or_conflict_claims",
        })
    manifest_entry = {
        "case_id": migrated.get("case_id"),
        "dataset": migrated.get("dataset"),
        "changes": changes,
        "security_contract_upgrade": new_type == "restricted",
        "pending_review_contract_upgrade": new_type == "conflict",
        "claim_granularity_upgrade": group is not None,
    }
    return migrated, manifest_entry


def migrate_cases(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    migrated: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []
    for case in cases:
        new_case, entry = migrate_case(case)
        migrated.append(new_case)
        manifest.append(entry)
    validate_cases(migrated)
    return migrated, manifest


def _load_directory(source: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for name in DATA_FILES:
        path = source / name
        with path.open(encoding="utf-8") as fh:
            cases.extend(json.loads(line) for line in fh if line.strip())
    return cases


def materialize_v2(source: Path, output: Path, *, overwrite: bool = False) -> dict[str, Any]:
    """Materialize v2 without changing source; source may be a v1 directory or zip."""
    source = source.resolve()
    source_hash = _sha256(source) if source.is_file() else None
    cleanup: Path | None = None
    if source.is_file():
        import zipfile

        cleanup = output.parent / f".{output.name}-v1-extracted"
        shutil.rmtree(cleanup, ignore_errors=True)
        with zipfile.ZipFile(source) as archive:
            archive.extractall(cleanup)
        candidates = list(cleanup.rglob(DATA_FILES[0]))
        if not candidates:
            raise FileNotFoundError(f"Layer 3 files are missing in {source}")
        source_dir = candidates[0].parent
    else:
        source_dir = source
    try:
        cases = _load_directory(source_dir)
        migrated, entries = migrate_cases(cases)
        if output.exists() and not overwrite:
            raise FileExistsError(f"destination exists: {output}; pass overwrite=True")
        output.mkdir(parents=True, exist_ok=True)
        by_dataset: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for case in migrated:
            by_dataset[str(case["dataset"])].append(case)
        output_files: dict[str, str] = {}
        for name in DATA_FILES:
            dataset = name.removesuffix(".jsonl")
            path = output / name
            path.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in by_dataset[dataset]), encoding="utf-8")
            output_files[name] = _sha256(path)
        payload = {
            "schema_version": "suixinji.layer3.contract_change_manifest.v2",
            "source_contract": "suixinji.layer3.retrieval_answer.v1",
            "target_contract": SCHEMA_VERSION,
            "source": str(source),
            "source_sha256": source_hash,
            "case_count": len(migrated),
            "output_files_sha256": output_files,
            "changes": entries,
        }
        (output / "contract_change_manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return payload
    finally:
        if cleanup:
            shutil.rmtree(cleanup, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Materialize Layer 3 v2 from immutable v1 data")
    parser.add_argument("--source", default="suixinji_layer3_datasets_v1.zip")
    parser.add_argument("--output", default="eval/layer3/data_v2")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    materialize_v2(Path(args.source), Path(args.output), overwrite=args.overwrite)


if __name__ == "__main__":
    main()
