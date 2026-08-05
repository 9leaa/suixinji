"""Run the Layer 2 concurrency fixtures against the real consolidator.

The deterministic evaluator processes candidates in fixture order.  This runner
instead releases all candidates at a barrier and checks the properties that
must hold regardless of which contender acquires the SQLite write lock first:
one active record per key, monotonic unique versions, unique source links and
space isolation.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from .adapter import Stage2EvalAdapter
    from .metrics import _case_final_exact
except ImportError:
    from eval.layer2.adapter import Stage2EvalAdapter
    from eval.layer2.metrics import _case_final_exact

from memory import repository
from memory.consolidator import consolidate_candidate


def _load_rows(data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / "version_source_idempotency.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and "concurrency" in line]


def _run_case(case: dict[str, Any], run_id: str, db_path: Path, repeat: int) -> dict[str, Any]:
    adapter = Stage2EvalAdapter(case, db_path, run_id)
    adapter.reset_case()
    adapter.seed_existing_memories()
    raws = list(case["input"].get("incoming_candidates", []))
    barrier = Barrier(len(raws))

    def worker(raw: dict[str, Any]) -> dict[str, Any]:
        candidate = adapter.build_candidate(raw)
        barrier.wait(timeout=30)
        try:
            result = consolidate_candidate(adapter.space_id, candidate.note_id or raw["candidate_id"], candidate)
            return {"raw": raw, "result": result}
        except Exception as exc:  # preserve lock/transaction failures in the report
            return {"raw": raw, "result": {}, "error": {"type": type(exc).__name__, "message": str(exc)}}

    with ThreadPoolExecutor(max_workers=len(raws), thread_name_prefix="layer2-concurrency") as pool:
        outcomes = list(pool.map(worker, raws))

    normalized: list[dict[str, Any]] = []
    for outcome in outcomes:
        raw = outcome["raw"]
        result = outcome.get("result") or {}
        adapter._map_result_ids(str(raw["candidate_id"]), result)
        observed = {"result": result, "decision": adapter._decision_for(str(raw["candidate_id"])), "error": outcome.get("error")}
        normalized.append(adapter.normalize_decision(raw, observed))
    state = adapter.snapshot_state()
    raw_by_id = {str(row["candidate_id"]): row for row in raws}
    for decision in normalized:
        raw = raw_by_id[str(decision["candidate_id"])]
        target = next((row for row in state["active_memories"] if row["memory_ref"] == decision.get("target_memory_ref")), None)
        if target:
            decision["final_memory_type"] = target["memory_type"]
            decision["final_task_status"] = target["task_status"]
            decision["expected_version_sequence"] = target["version_sequence"]
            decision["create_version"] = decision.get("action") in {"insert", "update"}
            decision["source_link_added"] = str(raw.get("note_id") or raw.get("candidate_id")) in target.get("source_note_ids", [])
        else:
            decision["create_version"] = False
            decision["source_link_added"] = False

    with repository._connect() as conn:
        duplicate_versions = conn.execute(
            "SELECT memory_id, version, COUNT(*) AS n FROM memory_versions GROUP BY memory_id, version HAVING n > 1"
        ).fetchall()
        duplicate_sources = conn.execute(
            "SELECT memory_id, note_id, COUNT(*) AS n FROM memory_sources GROUP BY memory_id, note_id HAVING n > 1"
        ).fetchall()
        foreign_spaces = conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE space_id != ?", (adapter.space_id,)
        ).fetchone()["n"]
    invariants = {
        "errors": len(adapter.errors) + sum(bool(outcome.get("error")) for outcome in outcomes),
        "duplicate_active_count": state["duplicate_active_count"],
        "stale_active_count": state["stale_active_count"],
        "duplicate_version_rows": len(duplicate_versions),
        "duplicate_source_rows": len(duplicate_sources),
        "foreign_space_rows": int(foreign_spaces),
    }
    invariants["pass"] = all(value == 0 for key, value in invariants.items() if key != "pass")
    result = {
        "case_id": case["case_id"],
        "repeat": repeat,
        "coverage_tags": case.get("coverage_tags", []),
        "predicted_decisions": normalized,
        "predicted_state": state,
        "gold": case["expected_output"],
        "invariants": invariants,
        "case_exact": _case_final_exact({"gold": case["expected_output"], "predicted_state": state}),
        "errors": adapter.errors,
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    rows = _load_rows(args.data_dir)
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="layer2-concurrency-") as temp_dir:
        for repeat in range(1, max(1, args.repeat) + 1):
            for index, case in enumerate(rows):
                results.append(_run_case(case, f"concurrency_{repeat}_{index}", Path(temp_dir) / f"case_{repeat}_{index}.db", repeat))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "layer2_concurrency_results.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results), encoding="utf-8"
    )
    passed = sum(row["invariants"]["pass"] for row in results)
    summary = {
        "cases": len(results),
        "invariant_pass_count": passed,
        "invariant_pass_rate": passed / len(results) if results else 0.0,
        "case_exact_count": sum(row["case_exact"] for row in results),
        "errors": sum(row["invariants"]["errors"] for row in results),
        "duplicate_active_cases": sum(row["invariants"]["duplicate_active_count"] > 0 for row in results),
        "duplicate_version_cases": sum(row["invariants"]["duplicate_version_rows"] > 0 for row in results),
        "duplicate_source_cases": sum(row["invariants"]["duplicate_source_rows"] > 0 for row in results),
        "foreign_space_cases": sum(row["invariants"]["foreign_space_rows"] > 0 for row in results),
    }
    (args.output_dir / "layer2_concurrency_metrics.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output_dir / "layer2_concurrency_summary.md").write_text(
        "# Layer 2 concurrency / idempotence report\n\n"
        f"- Cases: {summary['cases']}\n"
        f"- Invariant pass rate: {summary['invariant_pass_rate'] * 100:.2f}%\n"
        f"- Errors: {summary['errors']}\n"
        f"- Duplicate active cases: {summary['duplicate_active_cases']}\n"
        f"- Duplicate version cases: {summary['duplicate_version_cases']}\n"
        f"- Duplicate source cases: {summary['duplicate_source_cases']}\n"
        f"- Cross-space contamination cases: {summary['foreign_space_cases']}\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
