# ruff: noqa: E701, E702
#!/usr/bin/env python3
"""Data-only validator for the generated evaluation package."""
from __future__ import annotations
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from collections import Counter

ROOT = Path(__file__).resolve().parent / "suixinji_evaluation_v1"

def read(part):
    paths = [ROOT / part / "cases.jsonl"] if part != "l3" else sorted(path for path in (ROOT / part).glob("*.jsonl") if path.name != "cases.jsonl")
    return [json.loads(x) for path in paths for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]

def main() -> int:
    errors=[]; counts={}
    parts={k:read(k) for k in ["l1","l2","l3","l1_l2_bridge","l1_l2_l3_full"]}
    for name, rows in parts.items():
        counts[name]=len(rows)
        ids=[r.get("case_id") for r in rows]
        errors += [f"{name}:duplicate:{x}" for x,n in Counter(ids).items() if n>1]
        for row in rows:
            if not row.get("world_id"): errors.append(f"{name}:{row.get('case_id')}:missing_world_id")
    for row in parts["l1"]:
        text=row["input"]["current_message"]
        for c in row["expected"].get("candidates",[]):
            span=(c.get("evidence_span") or {}).get("text")
            if span and span not in text: errors.append(f"l1:{row['case_id']}:evidence_not_substring")
            if c.get("task_status") not in {None,"todo","done"}: errors.append(f"l1:{row['case_id']}:bad_task_status")
    for row in parts["l2"]:
        for d in row["expected"].get("decisions",[]):
            if d.get("action") not in {"insert","add_source","update","pending_review"}: errors.append(f"l2:{row['case_id']}:bad_action")
            if d.get("relation") not in {"new","same","merge","update","supersede","conflict"}: errors.append(f"l2:{row['case_id']}:bad_relation")
    for row in parts["l3"]:
        snapshot=row["input"]["memory_snapshot"]
        memories={x["memory_ref"] for x in snapshot.get("memories",[])}
        versions={x["version_ref"] for x in snapshot.get("versions",[])}
        sources={x["source_ref"] for x in snapshot.get("sources",[])}
        for version in snapshot.get("versions",[]):
            if version.get("memory_ref") not in memories: errors.append(f"l3:{row['case_id']}:version_missing_memory")
            if not set(version.get("source_refs",[])) <= sources: errors.append(f"l3:{row['case_id']}:version_missing_source")
        for memory in snapshot.get("memories",[]):
            if not set(memory.get("source_refs",[])) <= sources: errors.append(f"l3:{row['case_id']}:memory_missing_source")
        expected=row["expected"]
        if not set(expected.get("relevant_current_refs",[])) <= memories: errors.append(f"l3:{row['case_id']}:gold_missing_memory")
        if not set(expected.get("relevant_history_refs",[])) <= versions: errors.append(f"l3:{row['case_id']}:gold_missing_version")
        if not set(expected.get("required_citation_refs",[])) <= sources: errors.append(f"l3:{row['case_id']}:gold_missing_source")
    for row in parts["l1_l2_bridge"]:
        turn_ids={x["turn_id"] for x in row["input"]["turns"]}
        decisions=row["expected_l2"].get("decisions",[])
        if {x.get("turn_id") for x in decisions} != turn_ids: errors.append(f"bridge:{row['case_id']}:decision_turn_mismatch")
        snapshot=row["expected_l2"]["final_snapshot"]
        mrefs={x["ref"] for x in snapshot.get("memories",[])}; srefs={x["ref"] for x in snapshot.get("sources",[])}
        for version in snapshot.get("versions",[]):
            if version.get("memory_ref") not in mrefs: errors.append(f"bridge:{row['case_id']}:version_missing_memory")
            if not set(version.get("source_refs",[])) <= srefs: errors.append(f"bridge:{row['case_id']}:version_missing_source")
    from eval.layer3.contracts.v2 import validate_cases
    try: validate_cases(parts["l3"])
    except Exception as exc: errors.append(f"l3:contract:{exc}")
    world_ids=[r["world_id"] for rows in parts.values() for r in rows]
    split=json.loads((ROOT/"world_splits.json").read_text(encoding="utf-8"))
    assigned=[x for entry in split.values() for x in entry["world_ids"]]
    if len(assigned)!=len(set(assigned)): errors.append("split:duplicate_world_assignment")
    if set(assigned) != set(world_ids): errors.append("split:not_all_worlds_assigned")
    report={"status":"valid" if not errors else "invalid","counts":counts,"checks":{"json_schema":not errors,"evidence_substring":not any("evidence" in e for e in errors),"reference_integrity":not any("missing_" in e or "mismatch" in e for e in errors),"world_split_leakage":not any("split" in e for e in errors)},"errors":errors}
    (ROOT/"validation_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if not errors else 1
if __name__=="__main__": sys.exit(main())
