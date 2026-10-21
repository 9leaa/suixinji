"""Artifact-only final handoff; never splices targeted fixes into a complete score."""

import json
from cases import OUT, CASES

complete = "162147"
assessed = json.loads((OUT / f"assessed-summary-{complete}.json").read_text())
raw = json.loads((OUT / f"results-{complete}.json").read_text())
attempts = []
labels = {
    "154700": "initial complete",
    "155439": "framework-affected subset: unmasked Note reconstruction",
    "155646": "provider-affected subset: HTTP402 insufficient balance",
    "160145": "valid initial failure subset",
    "160313": "valid intermediate complete 18/20",
    "161234": "residual product fixes subset 2/2",
    "161452": "framework-affected complete: approval source scope and quote validator",
    "162027": "framework-corrected subset 4/4",
    "162147": "last complete business run; evidence assessed 18/20",
    "162822": "post-complete targeted R211 pass / R219 fail",
    "163100": "post-chronology-fix targeted R219 pass",
}
for run, label in labels.items():
    obj = json.loads((OUT / f"results-{run}.json").read_text())
    rows = obj["results"]
    attempts.append(
        {
            "run_id": run,
            "label": label,
            "raw_pass": sum(r["status"] == "pass" for r in rows),
            "total": len(rows),
            "failed": [r["id"] for r in rows if r["status"] != "pass"],
            "metadata": str(OUT / f"metadata-{run}.json"),
            "results": str(OUT / f"results-{run}.json"),
        }
    )
post = []
for run, ident in [("162822", "R211"), ("162822", "R219"), ("163100", "R219")]:
    obj = json.loads((OUT / f"results-{run}.json").read_text())
    r = next(x for x in obj["results"] if x["id"] == ident)
    post.append(
        {
            "run_id": run,
            "case_id": ident,
            "status": r["status"],
            "stages": r["stages"],
            "trace": str(OUT / "traces" / f"{ident}-{run}.json"),
            "source_sha256": obj["metadata"]["source_sha256"],
        }
    )
summary = {
    "round": 2,
    "initial_complete": {
        "run_id": "154700",
        "raw_pass": 14,
        "assessed_pass": 15,
        "total": 20,
        "raw_rate": 0.7,
        "assessed_rate": 0.75,
        "audit": str(OUT / "judge-audits/R213-154700.json"),
    },
    "final_complete": {
        "run_id": complete,
        "raw_pass": 16,
        "assessed_pass": 18,
        "total": 20,
        "raw_rate": 0.8,
        "assessed_rate": 0.9,
        "fixed_message_count": 113,
        "failed_cases": ["R211", "R219"],
        "stage_counts": assessed["stage_counts"],
        "metadata_path": str(OUT / f"metadata-{complete}.json"),
        "raw_results_path": str(OUT / f"results-{complete}.json"),
        "assessed_results_path": str(OUT / f"assessed-results-{complete}.json"),
        "assessed_summary_path": str(OUT / f"assessed-summary-{complete}.json"),
        "frozen_path": str(OUT / f"frozen-{complete}.json"),
        "source_sha256": raw["metadata"]["source_sha256"],
        "note": "same 20 business cases; assessed from actual trace/IDs and frozen gold, not a new execution or a human review",
    },
    "post_complete_targeted_runs": post,
    "targeted_results_not_added_to_complete_numerator": True,
    "latest_targeted_status": {"R211": "pass in 162822", "R219": "pass in 163100"},
    "attempts": attempts,
    "framework_invalid_runs": [
        {
            "run_id": "155439",
            "affected_cases": ["R215"],
            "reason": "ID-based raw Note reconstruction replaced true post-mask tool content",
            "audit": str(OUT / "framework-audit-R215-155439.json"),
        },
        {
            "run_id": "161452",
            "affected_cases": ["R215", "R218"],
            "reason": "approval simulator selected a relation-reference to JL508 inside JL509; quote validator ignored actual-record original_text/evidence_span",
            "audit": str(OUT / "audit-161452.json"),
        },
    ],
    "provider_affected_runs": [
        {
            "run_id": "155646",
            "affected_cases": ["R208", "R210", "R215", "R216"],
            "reason": "HTTP402 insufficient balance in pooled key; kept separate from semantic acceptance",
            "audit": str(OUT / "provider-incident-155646.json"),
        }
    ],
    "validation": {
        "database": "suixinji_fact_memory_test_20261020",
        "models": raw["metadata"]["models"],
        "embedding": raw["metadata"]["embedding"],
        "semantic_search_enabled": True,
        "source_drift_in_final_complete": json.loads((OUT / f"frozen-{complete}.json").read_text())["disk_changes_after_process_import"],
        "note_count": 113,
        "all_notes_preserved": True,
        "actual_extraction_contexts": 113,
        "hybrid_retrieval_runs": 359,
        "degraded_retrieval_runs": 0,
        "captured_answer_tool_calls": 44,
        "verified_support_quotes": 318,
        "uncaptured_selected_ids": 0,
        "structure_cases_in_accuracy_denominator": 0,
        "human_review_count": 0,
    },
    "fixed_dataset_sha256": raw["metadata"]["dataset_sha256"],
    "evidence_artifacts": [
        str(OUT / n)
        for n in [
            "dataset-freeze.json",
            "notes-preserved-162147.json",
            "evidence-verification-162147.json",
            "extraction-contexts-162147.json",
            "hybrid-retrieval-162147.json",
            "initial-diagnosis.json",
        ]
    ],
    "evaluation_code_only_edited_by_this_agent": True,
    "boundaries": [
        "R202答案对stale历史额外要求核实，已陈述全部正确事实，记录直接性边界。",
        "R216无绳岩壁与抱石术语可能有歧义，答案给完整实际规则并限定范围，不强迫术语等同。",
        "final complete source与定向修复source不同；没有再次全20验证最新source，不宣称最终完整20/20。",
    ],
    "subjects": [{"id": c["id"], "title": c["title"], "category": c["category"]} for c in CASES],
}
(OUT / "final-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
(OUT / "round2-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
print(
    json.dumps(
        {
            "final_complete_assessed": "18/20=90%",
            "latest_targeted_status": summary["latest_targeted_status"],
            "manifest": str(OUT / "final-summary.json"),
            "invalid_framework_runs": ["155439", "161452"],
            "provider_affected_runs": ["155646"],
        },
        ensure_ascii=False,
        indent=2,
    )
)
