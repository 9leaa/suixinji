"""Freeze one same-process, same-source complete round without blending reruns."""

import json
import sys
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from cases import OUT, CASES
from grade import STAGES

run = sys.argv[1]
r = json.loads((OUT / f"results-{run}.json").read_text())
rows = r["results"]
meta = r["metadata"]
assert {x["id"] for x in rows} == {c["id"] for c in CASES}, "not a complete round"
for name, h in meta["dataset_sha256"].items():
    assert hashlib.sha256((OUT / name).read_bytes()).hexdigest() == h, "dataset drift"
assert hashlib.sha256((Path(__file__).parent / "run.py").read_bytes()).hexdigest() == meta["evaluator_sha256"], "runner drift"
assert hashlib.sha256((Path(__file__).parent / "grade.py").read_bytes()).hexdigest() == meta["grader_sha256"], "grader drift"
changed = {
    p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
    for p, h in meta["source_sha256"].items()
    if hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != h
}
summary = {
    "run_id": run,
    "same_process_complete_run": True,
    "case_count": len(rows),
    "real_message_count": meta["message_count"],
    "pass": sum(x["status"] == "pass" for x in rows),
    "total": len(rows),
    "rate": sum(x["status"] == "pass" for x in rows) / len(rows),
    "failed_cases": [x["id"] for x in rows if x["status"] != "pass"],
    "stage_counts": {
        k: {
            "pass": sum(x.get("stages", {}).get(k, {}).get("passed") is True for x in rows),
            "fail": sum(x.get("stages", {}).get(k, {}).get("passed") is False for x in rows),
            "not_run": sum(not isinstance(x.get("stages", {}).get(k, {}).get("passed"), bool) for x in rows),
        }
        for k in STAGES
    },
    "source_sha256": meta["source_sha256"],
    "disk_changes_after_process_import": changed,
    "database": meta["database"],
    "dataset_sha256": meta["dataset_sha256"],
    "models": meta["models"],
    "human_review_count": 0,
    "structural_cases_in_accuracy_denominator": 0,
    "note": "保留模型自动判定原始结果；复核或失败项重跑另存，不能拼成同一完整轮。",
}
(OUT / f"frozen-{run}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
print(json.dumps(summary, ensure_ascii=False, indent=2))
