"""Regrade saved outputs after correcting grader bugs; never rerun business logic."""

import sys
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from independent_cases import OUT, CASES
from independent_grade import grade, add_answer_records

run = sys.argv[1]
check = json.loads((OUT / "checkpoints.json").read_text())
(OUT / "assessments").mkdir(exist_ok=True)


def work(case):
    p = OUT / "traces" / f"{case['id']}-{run}.json"
    r = json.loads(p.read_text())
    e = r.get("evidence", {})
    if not case["level"].startswith("真实") or not e.get("messages"):
        return r
    r["scorer_revision"] = "v2-actual-answer-evidence"
    r["original_scoring"] = {k: r.get(k) for k in ["status", "reason", "stages", "issues"]}
    add_answer_records(r["space"], e, r["final_state"])
    try:
        j = grade(case, e, check[case["id"]])
        r.update(status="pass" if j["passed"] else "fail", reason=j["reason"], stages=j["stages"], issues=j.get("issues", []))
        e["revised_judge"] = j
    except Exception as exc:
        r.update(status="blocked", reason="评测评分调用失败（不算业务失败）：" + str(exc))
    (OUT / "assessments" / f"{case['id']}-{run}.json").write_text(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({"id": r["id"], "status": r["status"], "reason": r["reason"]}, ensure_ascii=False), flush=True)
    return r


results = []
with ThreadPoolExecutor(max_workers=3) as pool:
    for f in as_completed(
        [
            pool.submit(work, c)
            for c in CASES
            if (OUT / "traces" / f"{c['id']}-{run}.json").exists() and (len(sys.argv) < 3 or c["id"] in sys.argv[2:])
        ]
    ):
        results.append(f.result())
base = json.loads((OUT / f"results-{run}.json").read_text())
base["results"] = sorted(results, key=lambda r: r["id"])
base["metadata"]["assessment_revision"] = "v2-actual-answer-evidence"
base["metadata"]["scorer_fixes"] = [
    "按问答实际工具TopK证据判召回，不以外部limit50替代",
    "不要求task/preference进入fm_card",
    "不要求所有场景都写历史，不因合法Note证据扣分；主体错配仍扣分",
]
(OUT / f"results-{run}-assessed.json").write_text(json.dumps(base, ensure_ascii=False, indent=2, default=str))
(OUT / "results.json").write_text(json.dumps(base, ensure_ascii=False, indent=2, default=str))
