"""Expanded PostgreSQL idempotency/concurrency evaluation using real repository code."""
from __future__ import annotations

import copy
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path("/home/zcj/suixinji")
sys.path.insert(0, str(ROOT))
import eval.layer2.run_postgres_concurrency_eval as base  # noqa: E402


def load() -> list[dict[str, Any]]:
    return [json.loads(line) for line in base.DATA.read_text(encoding="utf-8").splitlines() if line.strip()]


def clone(case: dict[str, Any], suffix: str) -> dict[str, Any]:
    value = copy.deepcopy(case)
    value["case_id"] = f"{value['case_id']}_{suffix}"
    value["coverage_tags"] = list(value.get("coverage_tags") or []) + ["cross_space_isolation"]
    return value


def main() -> None:
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=True)
    base.OUT = output
    base.PREFIX = f"layer2_pg_extended_{int(time.time())}_"
    rows = load()
    by_tag = {tag: [row for row in rows if tag in row.get("coverage_tags", [])] for tag in ["duplicate_delivery", "same_new_source", "update_version", "concurrent_conflict"]}
    results: list[dict[str, Any]] = []
    try:
        # The already-required 20 concurrency cases x 3 repeat: concurrent_same (10) + concurrent_conflict (10).
        concurrent = [row for row in rows if "concurrency" in row.get("coverage_tags", [])]
        for repeat in range(1, 4):
            for case in concurrent:
                result = base.run_case(case, repeat)
                result["scenario"] = "20_concurrent_cases_x3"
                results.append(result)
        # Full data-backed coverage for duplicate message, same-key/source changes and update versions.
        for scenario, cases in [("duplicate_delivery", by_tag["duplicate_delivery"]), ("same_key_new_source", by_tag["same_new_source"]), ("same_key_update", by_tag["update_version"])]:
            for case in cases:
                result = base.run_case(case, 1)
                result["scenario"] = scenario
                results.append(result)
        # Cross-space pairs use identical logical payloads concurrently. A result must not share physical memory ids.
        for index, case in enumerate(by_tag["update_version"]):
            left, right = clone(case, f"iso{index:02d}a"), clone(case, f"iso{index:02d}b")
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="pg-cross-space") as pool:
                a, b = list(pool.map(lambda item: base.run_case(item, 1), [left, right]))
            a_ids = {row["memory_id"] for row in a["predicted_state"]["all_memories"]}
            b_ids = {row["memory_id"] for row in b["predicted_state"]["all_memories"]}
            isolation = not bool(a_ids & b_ids)
            for item, peer in [(a, b), (b, a)]:
                item["scenario"] = "cross_space_isolation"
                item["cross_space_pair"] = peer["case_id"]
                item["invariants"]["foreign_space_rows"] = 0 if isolation else len(a_ids & b_ids)
                item["invariants"]["pass"] = item["invariants"]["pass"] and isolation
                results.append(item)
    finally:
        base.cleanup(base.PREFIX)
    failures = [row for row in results if not row["invariants"]["pass"]]
    summary = {
        "backend": "postgresql", "total_scenarios": len(results),
        "scenario_counts": {scenario: sum(row.get("scenario") == scenario for row in results) for scenario in sorted({str(row.get("scenario")) for row in results})},
        "invariant_pass_count": len(results) - len(failures), "invariant_pass_rate": (len(results) - len(failures)) / len(results) if results else 0,
        "errors": sum(row["invariants"]["errors"] for row in results),
        "duplicate_active_cases": sum(row["invariants"]["duplicate_active_count"] > 0 for row in results),
        "duplicate_version_cases": sum(row["invariants"]["duplicate_version_rows"] > 0 for row in results),
        "duplicate_source_cases": sum(row["invariants"]["duplicate_source_rows"] > 0 for row in results),
        "cross_space_contamination_cases": sum(row["invariants"]["foreign_space_rows"] > 0 for row in results),
        "temporary_space_prefix": base.PREFIX,
    }
    (output / "results.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results), encoding="utf-8")
    (output / "failures.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in failures), encoding="utf-8")
    (output / "metrics.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "summary.md").write_text("\n".join([
        "# PostgreSQL expanded concurrency / idempotence evaluation", "",
        "- Backend: real PostgreSQL; every scenario uses an isolated temporary space and is cleaned after snapshots.",
        "- `20_concurrent_cases_x3`: concurrent_same + concurrent_conflict, 20 cases × 3 repeats.",
        "- Extended: full duplicate-delivery (10), same-key new-source (10), same-key update (10), and 10 concurrent cross-space pairs (20 results).",
        f"- Total scenario results: {summary['total_scenarios']}; invariant pass: {summary['invariant_pass_rate']:.2%}",
        f"- Errors / duplicate active / duplicate versions / duplicate sources / cross-space contamination: {summary['errors']} / {summary['duplicate_active_cases']} / {summary['duplicate_version_cases']} / {summary['duplicate_source_cases']} / {summary['cross_space_contamination_cases']}",
        "- Raw per-run decisions, state snapshots and failures are retained in JSONL.", "",
    ]), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
