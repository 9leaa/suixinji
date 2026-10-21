"""Explicit assistant evidence spot-checks; never represented as human review."""

import json
from independent_cases import OUT

RUN = "150847"
OVERRIDES = {
    "FM01": {
        "reason": "实际回答明确声称没有记录说明豆豆是否随迁，但原消息m2、fact_7f34567961c9cd06c0f91794和事件卡fm_92b8a766e423b25c22215611均明确豆豆随用户搬到上海。真实问答fast_path限定semantic，仅取4条，漏掉事件证据。",
        "stages": {
            "retrieval": {"passed": False, "reason": "问答实际memory_type=semantic、limit=5，仅取4条；遗漏搬家事件与豆豆随迁事实。"},
            "answer": {"passed": False, "reason": "“记录里没有说明豆豆是否跟你一起搬到上海”与已保存原文和事实相反。"},
        },
        "suggestion": "混合问题取跨semantic/episodic证据，回答不得把类型过滤导致的缺证说成原文无记录。",
    },
    "FM16": {
        "reason": "原文的东京部署主体是星河服务，补充“我说的是假设，目前仍是东京”延续服务话题；实际fact及新卡改写成“用户目前仍在东京”，产生无证据的本人位置。",
        "stages": {
            "extraction": {"passed": False, "reason": "fact.content“用户目前仍在东京”把服务部署位置变成用户位置，主体错配。"},
            "consolidation": {"passed": False, "reason": "活跃卡“用户目前仍在东京”延续了无证据的主体错配。"},
        },
        "suggestion": "抽取与整理保留前文服务主体；无充分上下文时标待确定，不把省略主体直接归为用户。",
    },
}
for ident, v in OVERRIDES.items():
    p = OUT / "assessments" / f"{ident}-{RUN}.json"
    r = json.loads(p.read_text())
    r["pre_spot_check_scoring"] = {k: r.get(k) for k in ["status", "reason", "stages", "issues"]}
    r.update(
        status="fail",
        reason=v["reason"],
        assistant_evidence_review={"reviewer": "independent Codex subagent", "human_review": False, "reason": v["reason"]},
    )
    r["stages"].update(v["stages"])
    r["issues"] = [{"category": "主体或证据链错误", "evidence": v["reason"], "suggestion": v["suggestion"]}]
    p.write_text(json.dumps(r, ensure_ascii=False, indent=2))
base = json.loads((OUT / f"results-{RUN}-assessed.json").read_text())
by = {r["id"]: r for r in base["results"]}
for ident in OVERRIDES:
    by[ident] = json.loads((OUT / "assessments" / f"{ident}-{RUN}.json").read_text())
base["results"] = sorted(by.values(), key=lambda r: r["id"])
base["metadata"]["assistant_spot_checks"] = list(OVERRIDES)
base["metadata"]["human_review_count"] = 0
for name in ["results.json", f"results-{RUN}-assessed.json"]:
    (OUT / name).write_text(json.dumps(base, ensure_ascii=False, indent=2))
(OUT / "assistant-evidence-review.json").write_text(json.dumps(OVERRIDES, ensure_ascii=False, indent=2))
print("reviewed", list(OVERRIDES))
