"""Run all Layer-2 validated-candidate cases against isolated real PostgreSQL spaces.

Each case retains its fixture processing order. Different cases run in parallel
because they have distinct temporary Postgres spaces, which are deleted after
their snapshot is captured.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.layer2.adapter import _candidate, _seed_candidate  # noqa: E402
from eval.layer2.mappings import normalize_action, normalize_relation  # noqa: E402
from eval.layer2.metrics import evaluate  # noqa: E402
from infrastructure.database import session_scope  # noqa: E402
from infrastructure.schema import Memory, Space  # noqa: E402
from memory import repository  # noqa: E402
from memory.consolidator import consolidate_candidate  # noqa: E402
from repositories.postgres.common import parse_datetime  # noqa: E402
from repositories.postgres.memory import _add_version  # noqa: E402


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _record_dict(record: Any, ref: str) -> dict[str, Any]:
    return {
        "memory_ref": ref,
        "memory_id": record.id,
        "memory_type": record.memory_type,
        "memory_key": record.memory_key,
        "entity": record.subject,
        "attribute": record.predicate,
        "operation": record.scope.get("operation"),
        "canonical_topic": record.scope.get("canonical_topic"),
        "task_status": record.task_status,
        "old_value": record.scope.get("old_value"),
        "new_value": record.scope.get("new_value"),
        "content": record.content,
        "status": record.status,
        "version_sequence": record.current_version,
        "source_note_ids": sorted({source.note_id for source in record.sources}),
        "valid_from": record.valid_from,
        "valid_until": record.valid_until,
        "polarity": record.polarity,
        "updated_at": record.updated_at,
    }


class PgCaseAdapter:
    def __init__(self, case: dict[str, Any], space_id: str) -> None:
        self.case = case
        self.space_id = space_id
        self.logical_to_db: dict[str, str] = {}
        self.db_to_logical: dict[str, str] = {}
        self.errors: list[dict[str, Any]] = []

    def seed_existing(self) -> None:
        for raw in self.case["input"].get("existing_memories", []):
            ref = str(raw["memory_ref"])
            record = repository.insert_memory(
                self.space_id,
                _seed_candidate(raw),
                source_note_id=(raw.get("source_note_ids") or [f"seed:{ref}"])[0],
            )
            self.logical_to_db[ref] = record.id
            self.db_to_logical[record.id] = ref
            for note_id in (raw.get("source_note_ids") or [])[1:]:
                repository.add_source(record.id, str(note_id), "supported_by")
            target_version = max(1, int(raw.get("version_sequence") or 1))
            with session_scope() as session:
                row = session.get(Memory, record.id)
                if row is None:
                    raise RuntimeError(f"seeded memory disappeared: {record.id}")
                for version in range(2, target_version + 1):
                    row.current_version = version
                    _add_version(session, row, reason="layer2_eval_seed", source_note_id=None)
                row.current_version = target_version
                seed_time = parse_datetime(raw.get("updated_at") or row.updated_at)
                row.created_at = seed_time
                row.updated_at = seed_time

    def _map_result_ids(self, candidate_id: str, result: dict[str, Any]) -> None:
        memory_id = result.get("memory_id")
        if not memory_id or str(memory_id) in self.db_to_logical:
            return
        if result.get("action") in {"insert", "pending_review", "supersede", "conflict"}:
            ref = f"new:{candidate_id}"
            self.logical_to_db[ref] = str(memory_id)
            self.db_to_logical[str(memory_id)] = ref

    def _decision_for(self, candidate_id: str) -> dict[str, Any] | None:
        return next((row for row in repository.list_memory_decisions(self.space_id, limit=500) if row["candidate_id"] == candidate_id), None)

    def _normalize(self, raw: dict[str, Any], result: dict[str, Any], decision: dict[str, Any] | None, error: dict[str, Any] | None) -> dict[str, Any]:
        decision = decision or {}
        target_ids = decision.get("target_memory_ids") or []
        target_refs = [self.db_to_logical.get(str(item), f"db:{item}") for item in target_ids]
        raw_action = result.get("action") or decision.get("recommended_action")
        raw_relation = result.get("relation") or decision.get("relation")
        action = normalize_action(raw_action)
        relation = normalize_relation(raw_relation, raw_action)
        target_ref = None
        memory_id = result.get("memory_id")
        if memory_id:
            target_ref = self.db_to_logical.get(str(memory_id))
            if target_ref is None and action in {"insert", "pending_review", "supersede", "conflict"}:
                target_ref = f"new:{raw['candidate_id']}"
        if action == "pending_review":
            target_ref = None
        return {
            "candidate_id": str(raw["candidate_id"]),
            "matched_memory_refs": target_refs,
            "task_identity_match": bool(target_refs) if raw["memory_type"] == "task" else None,
            "relation": relation,
            "action": action,
            "target_memory_ref": target_refs[0] if target_refs else target_ref,
            "final_memory_type": None,
            "final_task_status": None,
            "create_version": None,
            "expected_version_sequence": None,
            "source_link_added": result.get("source_added"),
            "pending_review": action == "pending_review",
            "reason": decision.get("reason") or result.get("reason"),
            "error": error,
            "raw_result": _jsonable(result),
        }

    def snapshot(self) -> dict[str, Any]:
        records = repository.list_memories(self.space_id, status=None, include_expired=True, limit=500)
        all_records = [_record_dict(record, self.db_to_logical.get(record.id, f"db:{record.id}")) for record in records]
        active = [record for record in all_records if record["status"] == "active"]
        counts: dict[str, int] = {}
        for record in active:
            key = record["memory_key"] or record["memory_ref"]
            counts[key] = counts.get(key, 0) + 1
        return {
            "all_memories": all_records,
            "active_memories": active,
            "pending_review_memories": [record for record in all_records if record["status"] == "pending_review"],
            "expected_active_memory_refs": [record["memory_ref"] for record in active],
            "duplicate_active_count": sum(max(0, count - 1) for count in counts.values()),
            "stale_active_count": 0,
        }

    def run(self) -> dict[str, Any]:
        self.seed_existing()
        decisions: list[dict[str, Any]] = []
        raws = {str(raw["candidate_id"]): raw for raw in self.case["input"].get("incoming_candidates", [])}
        for candidate_id in self.case["input"].get("processing_order", []):
            raw = raws[str(candidate_id)]
            result: dict[str, Any] = {}
            error: dict[str, Any] | None = None
            try:
                candidate = _candidate(raw)
                result = consolidate_candidate(self.space_id, candidate.note_id or str(candidate_id), candidate)
                self._map_result_ids(str(candidate_id), result)
            except Exception as exc:
                error = {"type": type(exc).__name__, "message": str(exc), "candidate_id": str(candidate_id)}
                self.errors.append(error)
            decisions.append(self._normalize(raw, result, self._decision_for(str(candidate_id)), error))
        state = self.snapshot()
        active_by_ref = {row["memory_ref"]: row for row in state["active_memories"]}
        for decision in decisions:
            raw = raws[decision["candidate_id"]]
            target = active_by_ref.get(decision.get("target_memory_ref"))
            if target:
                decision["final_memory_type"] = target["memory_type"]
                decision["final_task_status"] = target["task_status"]
                decision["expected_version_sequence"] = target["version_sequence"]
                decision["create_version"] = decision.get("action") in {"insert", "update"}
                note_id = str(raw.get("note_id") or raw["candidate_id"])
                decision["source_link_added"] = note_id in target.get("source_note_ids", [])
            else:
                decision["create_version"] = False
                decision["source_link_added"] = False
        return {
            "case_id": self.case["case_id"],
            "dataset": self.case.get("dataset"),
            "space_id": self.space_id,
            "gold": self.case["expected_output"],
            "predicted_decisions": decisions,
            "predicted_state": state,
            "errors": self.errors,
            "input": self.case["input"],
            "coverage_tags": self.case.get("coverage_tags", []),
            "difficulty": self.case.get("difficulty"),
        }


def _cleanup_space(space_id: str) -> None:
    with session_scope() as session:
        session.execute(delete(Space).where(Space.source_space_id == space_id))


def _run_case(case: dict[str, Any], prefix: str) -> dict[str, Any]:
    space_id = f"{prefix}{case['case_id']}"
    adapter = PgCaseAdapter(case, space_id)
    try:
        return adapter.run()
    finally:
        _cleanup_space(space_id)


def _load(zip_path: Path) -> dict[str, list[dict[str, Any]]]:
    data: dict[str, list[dict[str, Any]]] = {}
    with zipfile.ZipFile(zip_path) as archive:
        for name in archive.namelist():
            if not name.endswith(".jsonl"):
                continue
            rows = [json.loads(line) for line in archive.read(name).decode("utf-8").splitlines() if line.strip()]
            if rows:
                data[Path(name).stem] = rows
    return dict(sorted(data.items()))


def _write_report(output_dir: Path, dataset: str, rows: list[dict[str, Any]], source_zip: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = evaluate(rows)
    (output_dir / "predictions.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    (output_dir / "case_exact_failures.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows if not row.get("case_exact")), encoding="utf-8"
    )
    (output_dir / "runtime_errors.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows if row.get("errors")), encoding="utf-8"
    )
    (output_dir / "metrics.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "dataset": dataset,
        "dataset_sha256": hashlib.sha256(source_zip.read_bytes()).hexdigest(),
        "backend": "postgresql_isolated_spaces",
        "mode": "validated_candidates_deterministic",
        "first_stage_extractor_called": False,
        "production_space_written": False,
        "case_count": len(rows),
        "parallelism": "case-level only; candidate order is preserved within every case",
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        f"# Layer 2 PostgreSQL 评测 — {dataset}",
        "",
        "- 输入：validated MemoryCandidate；未调用第一阶段抽取器或 LLM。",
        "- 后端：真实 PostgreSQL；每个 case 使用独立临时 Space，并在快照后删除。",
        "- 并发：case-level；每个 case 内严格按 fixture 的 `processing_order`。",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Task Identity F1 | {summary['task_identity']['f1']:.2%} |",
        f"| Relation Macro-F1 | {summary['relation_macro_f1']:.2%} |",
        f"| Action Accuracy | {summary['action_accuracy']:.2%} |",
        f"| Task Transition Accuracy | {summary['task_transition_accuracy']:.2%} |",
        f"| Version Sequence Accuracy | {summary['version_sequence_accuracy']:.2%} |",
        f"| Version Creation Accuracy | {summary['version_creation_accuracy']:.2%} |",
        f"| Idempotence Accuracy | {summary['idempotence_accuracy']:.2%} |",
        f"| Case Exact Match | {summary['case_exact_match']:.2%} |",
        f"| Pending-review F1 | {summary['pending_review']['f1']:.2%} |",
        f"| Source Link F1 | {summary['source_link']['f1']:.2%} |",
        f"| Source Exact-set Accuracy | {summary['source_exact_set_accuracy']:.2%} |",
        f"| Duplicate Active Rate | {summary['duplicate_active_rate']:.2%} |",
        f"| Stale Active Rate | {summary['stale_active_rate']:.2%} |",
        f"| Orphan Done Task Rate | {summary['orphan_done_task_rate']:.2%} |",
        f"| Runtime Error Cases | {sum(bool(row.get('errors')) for row in rows)} |",
    ]
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", type=Path, default=ROOT / "suixinji_layer2_datasets_v1.zip")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    datasets = _load(args.zip)
    prefix = f"eval_layer2_pg_{int(time.time())}_"
    results: dict[str, list[dict[str, Any]]] = {name: [] for name in datasets}
    jobs = [(dataset, case, index) for dataset, cases in datasets.items() for index, case in enumerate(cases)]
    with ThreadPoolExecutor(max_workers=max(1, args.workers), thread_name_prefix="layer2-pg") as pool:
        futures = {pool.submit(_run_case, case, prefix): (dataset, index) for dataset, case, index in jobs}
        indexed: list[tuple[str, int, dict[str, Any]]] = []
        for future in as_completed(futures):
            dataset, index = futures[future]
            indexed.append((dataset, index, future.result()))
    for dataset, _, result in sorted(indexed, key=lambda item: (item[0], item[1])):
        results[dataset].append(result)
    summaries: dict[str, Any] = {}
    all_rows: list[dict[str, Any]] = []
    for dataset, rows in results.items():
        summaries[dataset] = _write_report(args.output_dir / dataset, dataset, rows, args.zip)
        all_rows.extend(rows)
    all_summary = _write_report(args.output_dir / "all", "all_datasets", all_rows, args.zip)
    output = {
        "output_dir": str(args.output_dir),
        "backend": "postgresql",
        "workers": max(1, args.workers),
        "datasets": summaries,
        "all": all_summary,
        "temporary_space_prefix": prefix,
    }
    (args.output_dir / "all_metrics.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
