"""Validate the five Layer 2 JSONL datasets without running production code."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


EXPECTED_FILES = {
    "relation_and_action_core.jsonl": 120,
    "task_state_transition.jsonl": 144,
    "orphan_done_resolution.jsonl": 100,
    "version_source_idempotency.jsonl": 80,
    "non_task_consolidation.jsonl": 120,
}


def validate(data_dir: Path) -> dict[str, object]:
    reports: dict[str, object] = {}
    total = 0
    errors: list[str] = []
    for name, expected_count in EXPECTED_FILES.items():
        path = data_dir / name
        if not path.exists():
            errors.append(f"missing:{name}")
            continue
        rows = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"{name}:{line_number}:json:{exc}")
                continue
            for key in ("schema_version", "case_id", "input", "expected_output"):
                if key not in row:
                    errors.append(f"{name}:{line_number}:missing:{key}")
            rows.append(row)
        if len(rows) != expected_count:
            errors.append(f"{name}:expected={expected_count}:actual={len(rows)}")
        total += len(rows)
        reports[name] = {"rows": len(rows), "expected_rows": expected_count, "valid": len(rows) == expected_count}
    return {"schema_version": "suixinji.layer2.consolidation.v1", "total_cases": total, "expected_total": 564, "reports": reports, "errors": errors, "valid": not errors and total == 564}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    result = validate(args.data_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["valid"] else 1)


if __name__ == "__main__":
    main()
