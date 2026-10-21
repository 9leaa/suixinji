import json
import hashlib
from pathlib import Path
from cases import OUT

RUN = "163408"
d = json.loads((OUT / f"results-{RUN}.json").read_text())
assert len(d["results"]) == 20
rows = {r["id"]: r for r in d["results"]}
audit = []
overrides = []


def strings(obj, path="$"):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from strings(v, path + "." + k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from strings(v, path + f"[{i}]")


def proof(records, quote):
    for r in records:
        for path, value in strings(r):
            if quote in value:
                return {"record_id": r["id"], "field": path, "quote": quote}
    raise AssertionError("No actual record quote: " + quote)


def log(id, criterion, passed, records):
    audit.append({"id": id, "criterion": criterion, "passed": passed, "actual_record_proofs": records})


def correction(id, stage, passed, reason, records):
    r = rows[id]
    overrides.append(
        {
            "id": id,
            "stage": stage,
            "original": r["stages"].get(stage),
            "reviewed": {"passed": passed, "reason": reason},
            "actual_record_proofs": records,
        }
    )
    r["stages"][stage] = {"passed": passed, "reason": reason, "assistant_reviewed": True, "actual_record_proofs": records}


# R304: 180 is in the actual returned sibling fact's original_text, not reconstructed from Note.
e = rows["R304"]["evidence"]
ps = [proof(e["query_hits"], "苇岸那一场票价180元"), proof(e["actual_answer_records"], "苇岸那一场票价180元")]
log("R304", "两笔票价确在默认hits与真实问答payload", True, ps)
correction(
    "R304",
    "query_retrieval",
    True,
    "默认hits和实际问答均返回fact的original_text，完整含蒲灯260/苇岸180；不能仅检查content而误称缺180。",
    ps,
)
# Locked edit must constrain other newly consolidated cards too.
e = rows["R312"]["evidence"]
cards = [r for r in e["after_final"]["card"] if r["status"] == "active"]
locked = [r for r in cards if r.get("user_locked")]
assert any("30天" in r["content"] for r in locked)
bad = [r for r in cards if not r.get("user_locked") and "60天" in r["content"]]
log(
    "R312",
    "用户30天锁卡保留且其他有效新卡不复活60天",
    not bad,
    [proof(locked, "30天")] + [{"record_id": r["id"], "content": r["content"]} for r in bad],
)
# Both the task and independent current negative state must exist in actual structured content.
e = rows["R319"]["evidence"]
facts = [f for f in e["after_final"]["fact"] if f["note_id"].endswith("-m2") and f["status"] == "active"]
neg = [f for f in facts if "报价" in f["content"] and any(w in f["content"] for w in ["没有", "未", "没"])]
tasks = [
    t
    for t in e["legacy_memories"]
    if t["memory_type"] == "task" and t["task_status"] == "todo" and "白汀" in t["content"] and "10点" in t["content"]
]
log(
    "R319",
    "原task完整todo与m2独立未报价状态同时保留",
    bool(neg and tasks),
    [{"record_id": r["id"], "content": r["content"]} for r in neg + tasks],
)
assert neg and tasks
ps = [{"record_id": r["id"], "content": r["content"]} for r in neg + tasks]
correction("R319", "extraction", True, "m1完整任务保存在legacy todo，m2当前未报价已存在fact.content；不要求任务复制事实卡。", ps)
correction("R319", "consolidation", True, "完整todo保留原task路径，当前未完成/未报价在有效整理卡；不要求fm_card再造任务。", ps)
for stage, hits in [
    ("immediate_retrieval", e["immediate_probe"]["hits"]),
    ("consolidated_retrieval", e["consolidated_hits"]),
    ("query_retrieval", e["query_hits"]),
]:
    ps = [proof(hits, "报价"), proof(hits, "白汀")]
    present = any("报价" in r.get("content", "") and any(w in r.get("content", "") for w in ["没有", "未", "没"]) for r in hits)
    log("R319", stage + "同时覆盖具体todo与未报价", present, ps)
# Don't infer preference retrieval from storage or a correct-looking answer.
e = rows["R320"]["evidence"]
for stage, records in [
    ("immediate_retrieval", e["immediate_probe"]["hits"]),
    ("consolidated_retrieval", e["consolidated_hits"]),
    ("query_retrieval", e["query_hits"]),
    ("actual_answer_records", e["actual_answer_records"]),
]:
    ps = [proof(records, "糖霜"), proof(records, "没有改变")]
    log("R320", stage + "含表面处理与未改偏好的实际记录", True, ps)
# A passing judge must not hide an answer that denies a clearly recorded unchanged preference.
ps = [proof(e["actual_answer_records"], "试吃没有改变这一偏好")]
assert "没有明确说" in e["answer"] and "不能进一步推断" in e["answer"]
log(
    "R320",
    "回答应确认原消息明确说试吃未改变偏好，而非声称缺少明确原文",
    False,
    ps + [{"source_note_id": e["messages"][2]["note"]["id"], "original_text": e["messages"][2]["note"]["text"]}],
)
correction(
    "R320",
    "answer",
    False,
    "口味/不淋糖霜回答正确，但对试吃是否改变偏好又称原文没有明确说、不能推断；原m3明确未改变，实际偏好content也返回未改变，属于过度退让而漏答。",
    ps,
)
# Forget approvals may never include the adjacent entity's source.
e = rows["R314"]["evidence"]
state = e["after_final"]
fs = {r["id"]: r for r in state["fact"]}
cs = {r["id"]: r for r in state["card"]}
selected = []
for ident in e["forget_approved_ids"]:
    r = cs.get(ident) or fs[ident]
    source_ids = r.get("source_fact_ids", [ident])
    assert all(fs[f]["note_id"].endswith(("-m1", "-m2")) for f in source_ids)
    selected.append({"record_id": ident, "content": r["content"], "source_fact_ids": source_ids})
remaining = [r for r in e["after_operation"]["fact"] if r["status"] == "active" and "ZC-552" in r["content"]]
log(
    "R314",
    "批准只覆盖ZC551来源m1/m2，ZC552有效记忆保留",
    bool(remaining),
    selected + [{"record_id": r["id"], "content": r["content"]} for r in remaining],
)
for records in [e["consolidated_hits"], e["query_hits"], e["actual_answer_records"]]:
    assert not any("蓝色收件回执" in value or "11月3日9点20分" in value for record in records for _, value in strings(record))
log("R314", "默认hits和实际post-mask问答记录不泄漏忘记细节", True, [])
# Note-edit source revision must retire the old evidence, not merely hide its answer.
e = rows["R317"]["evidence"]
old = [
    f for f in e["after_operation"]["fact"] if f["note_id"].endswith("-m1") and "24小时" in f["content"] and "最长可提前24" in f["content"]
]
new = [f for f in e["after_operation"]["fact"] if f["note_id"].endswith("-m1") and f["status"] == "active" and "12小时" in f["content"]]
assert old and all(f["status"] == "source_changed" for f in old) and new
log(
    "R317",
    "来源同Note新版12小时生效，旧24小时fact全部source_changed",
    True,
    [{"record_id": f["id"], "status": f["status"], "content": f["content"]} for f in old + new],
)
for r in d["results"]:
    r["original_judge"] = r["evidence"].get("judge")
    r["original_status"] = r["status"]
    r["assistant_reviewed"] = any(x["id"] == r["id"] for x in audit)
    r["status"] = "pass" if all(v["passed"] for v in r["stages"].values()) else "fail"
    r["reason"] = "；".join(v["reason"] for v in r["stages"].values() if not v["passed"]) or "六阶段通过；实际ID/片段校验及关键通过项审计。"
d["metadata"]["assessment_only"] = True
d["metadata"]["assistant_review_script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
(OUT / "final-spot-audit.json").write_text(
    json.dumps({"run_id": RUN, "human_reviewed": False, "audit": audit, "overrides": overrides}, ensure_ascii=False, indent=2)
)
(OUT / f"reviewed-results-{RUN}.json").write_text(json.dumps(d, ensure_ascii=False, indent=2))
print(
    json.dumps(
        {
            "pass": sum(r["status"] == "pass" for r in d["results"]),
            "total": 20,
            "failed": [r["id"] for r in d["results"] if r["status"] != "pass"],
            "audit_all_passed": all(x["passed"] for x in audit),
        },
        ensure_ascii=False,
    )
)
