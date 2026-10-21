"""Report initial, failed-case rerun, and final complete round as separate cohorts."""

import json
import sys
from cases import OUT, CASES

initial_id, subset_id, final_id = sys.argv[1:4]


def read(run_id):
    return json.loads((OUT / f"results-{run_id}.json").read_text())


initial, subset, final = map(read, [initial_id, subset_id, final_id])
assert {r["id"] for r in final["results"]} == {c["id"] for c in CASES}


def summary(data):
    rows = data["results"]
    return {
        "run_id": data["metadata"]["run_id"],
        "pass": sum(x["status"] == "pass" for x in rows),
        "total": len(rows),
        "rate": sum(x["status"] == "pass" for x in rows) / len(rows),
        "failed": [x["id"] for x in rows if x["status"] != "pass"],
        "source_sha256": data["metadata"]["source_sha256"],
    }


by_runs = [{x["id"]: x for x in data["results"]} for data in [initial, subset, final]]
rows = []
for c in CASES:
    row = {
        "id": c["id"],
        "title": c["title"],
        "category": c["category"],
        "fixed_expected": c["expected"],
        "statuses": {},
        "failed_stages": {},
    }
    for label, by in zip(["initial", "failed_case_rerun", "final_complete"], by_runs):
        r = by.get(c["id"])
        row["statuses"][label] = r["status"] if r else "not_in_subset"
        row["failed_stages"][label] = [k for k, v in (r or {}).get("stages", {}).items() if v.get("passed") is False]
    rows.append(row)
report = {
    "initial_raw": summary(initial),
    "initial_evidence_audited": {"pass": 15, "total": 20, "rate": 0.75, "audit_changed": ["R213"], "raw_preserved": True},
    "failed_case_rerun": summary(subset),
    "final_complete": summary(final),
    "same_frozen_dataset": all(d["metadata"]["dataset_sha256"] == initial["metadata"]["dataset_sha256"] for d in [subset, final]),
    "rows": rows,
    "preserved_intermediate_attempts": [
        {
            "run_id": "155439",
            "raw_pass": 5,
            "total": 6,
            "validity": "R215工具返回被按ID重读raw Note，框架误报；原始文件完整保留",
            "audit": "framework-audit-R215-155439.json",
        },
        {
            "run_id": "155646",
            "raw_pass": 2,
            "total": 6,
            "validity": "HTTP402余额不足影响抽取/整理/合成，非语义验收轮",
            "audit": "provider-incident-155646.json",
        },
    ],
    "automatic": True,
    "human_reviewed": False,
    "note": "失败项重跑不拼入完整轮；最终准确率仅来自final_complete单一完整进程。初测R213经第二模型证据复核纠正，原judge保留。",
}
assert report["same_frozen_dataset"]
(OUT / "comparison-final.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False, indent=2))
