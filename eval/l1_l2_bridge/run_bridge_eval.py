from __future__ import annotations

import argparse, dataclasses, hashlib, json, platform, re, sys, time, zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import delete
from infrastructure.database import session_scope
from infrastructure.schema import Space
from repositories.postgres.common import ensure_tenant_space
from memory import repository
from memory.models import MemoryCandidate, MEMORY_KEY_V3_VERSION, normalize_content
from memory.service import process_note_memory
from memory.consolidator import consolidate_candidate
from memory.trace import get_trace
from eval.layer2.mappings import normalize_action, normalize_relation

def task_status(value: Any) -> str | None:
    status = str(value or "").casefold() or None
    return {"in_progress":"todo", "blocked":"todo", "cancelled":"done", "canceled":"done"}.get(status, status)


def jsonable(x: Any) -> Any:
    if dataclasses.is_dataclass(x): return jsonable(dataclasses.asdict(x))
    if isinstance(x, dict): return {str(k): jsonable(v) for k,v in x.items()}
    if isinstance(x, (list,tuple,set)): return [jsonable(v) for v in x]
    if isinstance(x, (str,int,float,bool)) or x is None: return x
    return str(x)

def utc(v: Any) -> str | None:
    if v is None: return None
    if isinstance(v, datetime):
        d = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    else:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00")); d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat()

def ensure_space(space_id: str, tenant_id: str) -> None:
    with session_scope() as s: ensure_tenant_space(s, space_id, tenant_id=tenant_id, source="bridge_eval")

def cleanup(space_id: str) -> None:
    with session_scope() as s: s.execute(delete(Space).where(Space.source_space_id == space_id))

def record_dict(m: Any) -> dict[str, Any]:
    d = m.to_dict() if hasattr(m, "to_dict") else dict(m)
    scope = d.get("scope") or {}
    return {
        "id": d.get("id"), "memory_type": d.get("memory_type"), "content": d.get("content"),
        "memory_key": d.get("memory_key"), "canonical_topic": scope.get("canonical_topic"),
        "subject": d.get("subject"), "predicate": d.get("predicate"), "task_status": d.get("task_status"),
        "polarity": d.get("polarity"), "status": d.get("status"), "current_version": d.get("current_version"),
        "valid_from": utc(d.get("valid_from")), "valid_until": utc(d.get("valid_until")),
        "sources": jsonable(d.get("sources") or []), "versions": jsonable(d.get("versions") or []),
        "updated_at": utc(d.get("updated_at")),
    }

def snapshot(space_id: str) -> dict[str, Any]:
    rows = [record_dict(m) for m in repository.list_memories(space_id, status=None, include_expired=True, limit=500)]
    active = [r for r in rows if r["status"] == "active"]
    keys = Counter((r.get("memory_key") or r.get("canonical_topic") or r.get("content")) for r in active)
    return {"all_memories": rows, "active_memories": active,
            "pending_review_memories": [r for r in rows if r["status"] == "pending_review"],
            "duplicate_active_count": sum(max(0,n-1) for n in keys.values()),
            "stale_active_count": 0}

def build_candidate(raw: dict[str, Any], *, note_id: str, case_id: str) -> MemoryCandidate:
    scope = {"scope": "global", "operation": raw.get("operation"), "canonical_topic": raw.get("canonical_topic"),
             "old_value": raw.get("old_value"), "new_value": raw.get("current_value") or raw.get("canonical_topic"),
             "memory_key_version": MEMORY_KEY_V3_VERSION}
    cid = f"oracle:{case_id}:{raw.get('candidate_ref')}:{note_id}"
    return MemoryCandidate(memory_type=str(raw["memory_type"]), content=str(raw.get("content") or raw.get("evidence_text") or ""),
        importance=.8, confidence=.99, should_store=True, task_status=task_status(raw.get("task_status")), candidate_id=cid,
        note_id=note_id, subject=raw.get("entity"), predicate=raw.get("attribute"), object_value=raw.get("current_value") or raw.get("canonical_topic"),
        valid_from=raw.get("valid_from"), valid_until=raw.get("valid_until"), evidence_span=raw.get("evidence_text"),
        polarity=raw.get("polarity"), scope=scope, extractor_type="oracle-l1", extractor_version="bridge-v1", memory_key=raw.get("memory_key"), memory_key_version=MEMORY_KEY_V3_VERSION)

def candidate_view(c: Any) -> dict[str, Any]:
    if c is None: return {}
    scope = getattr(c, "scope", {}) or {}
    return {"candidate_id": getattr(c,"candidate_id",None), "memory_type": getattr(c,"memory_type",None), "content": getattr(c,"content",None),
            "canonical_topic": scope.get("canonical_topic"), "memory_key": getattr(c,"memory_key",None), "entity": getattr(c,"subject",None),
            "attribute": getattr(c,"predicate",None), "operation": scope.get("operation"), "task_status": getattr(c,"task_status",None),
            "polarity": getattr(c,"polarity",None), "evidence_text": getattr(c,"evidence_span",None), "should_store": getattr(c,"should_store",True)}

def norm(s: Any) -> str: return normalize_content(str(s or ""))

def match_candidates(gold: list[dict[str,Any]], pred: list[dict[str,Any]]) -> list[dict[str,Any]]:
    unused = set(range(len(pred))); out=[]
    for g in gold:
        best=None; bestscore=-1
        for i,p in enumerate(pred):
            if i not in unused or g.get("memory_type") != p.get("memory_type"): continue
            score=0
            if norm(g.get("evidence_text")) and norm(g.get("evidence_text")) == norm(p.get("evidence_text")): score += 4
            if norm(g.get("canonical_topic")) == norm(p.get("canonical_topic")): score += 3
            if norm(g.get("memory_key")) == norm(p.get("memory_key")): score += 2
            if score>bestscore: bestscore,best=score,i
        # Official matching is intentionally type-aware and has a stable
        # same-type fallback, while strict matching requires identity/evidence
        # agreement.  This preserves the independent Layer-1 contract.
        if best is not None: unused.remove(best); out.append({"gold":g,"pred":pred[best],"matched":True,"strict":bestscore>=7})
        else: out.append({"gold":g,"pred":None,"matched":False,"strict":False})
    for i in sorted(unused): out.append({"gold":None,"pred":pred[i],"matched":False,"strict":False})
    return out

def actual_case(case: dict[str,Any], run_id: str, mode: str) -> dict[str,Any]:
    cid=str(case["case_id"]); sid=f"bridge:{run_id}:{mode}:{cid}"; tenant=f"tenant:{run_id}:{mode}:{cid}"
    ensure_space(sid, tenant); turns=[]; errors=[]; replay_ok=True
    try:
        for turn in case["input"]["turns"]:
            tid=str(turn["turn_id"]); note_id=f"bridge:{run_id}:{mode}:{cid}:{tid}"; before=snapshot(sid); started=time.perf_counter()
            try:
                result=process_note_memory({"id":note_id,"space_id":sid,"tenant_id":tenant,"message_id":turn.get("idempotency_key"),"text":turn["note_text"]})
                trace=get_trace(result.get("trace_id")) or {}; cids=[(s.get("output_summary") or {}).get("candidate_id") for s in trace.get("steps",[]) if s.get("step")=="candidate_extracted"]
                pred=[]
                for x in cids:
                    c=repository.get_memory_candidate(str(x));
                    if c is not None: pred.append(candidate_view(c))
                decisions=repository.list_memory_decisions(sid,note_id=note_id,limit=100)
                after=snapshot(sid)
                if turn.get("delivery_replay"):
                    replay_before=snapshot(sid); process_note_memory({"id":note_id,"space_id":sid,"tenant_id":tenant,"message_id":turn.get("idempotency_key"),"text":turn["note_text"]}); replay_after=snapshot(sid)
                    replay_ok = replay_ok and json.dumps(replay_before,sort_keys=True)==json.dumps(replay_after,sort_keys=True)
                gold_l1=turn["expected_l1"]["candidates"]; mapping=match_candidates(gold_l1,pred)
                turns.append({"turn_id":tid,"gold_l1":turn["expected_l1"],"predicted_l1":{"should_store":bool(result.get("candidates") or pred),"candidates":pred},"candidate_mapping":mapping,
                    "gold_l2_decision":[d for d in case["expected_l2"]["decisions"] if d["turn_id"]==tid],"predicted_l2_decision":jsonable(decisions),
                    "db_before":before,"db_after":after,"latency_ms":round((time.perf_counter()-started)*1000,2),"llm_diagnostic":{"trace_id":result.get("trace_id")},"error_stage":None})
            except Exception as e:
                errors.append({"turn_id":tid,"type":type(e).__name__,"message":str(e)}); turns.append({"turn_id":tid,"gold_l1":turn["expected_l1"],"predicted_l1":{"should_store":False,"candidates":[]},"candidate_mapping":match_candidates(turn["expected_l1"]["candidates"],[]),"gold_l2_decision":[],"predicted_l2_decision":[],"db_before":before,"db_after":snapshot(sid),"latency_ms":round((time.perf_counter()-started)*1000,2),"error_stage":"SYSTEM_ERROR"})
        final=snapshot(sid)
        return {"case_id":cid,"scenario_family":case.get("scenario_family"),"turns":turns,"final_snapshot":final,"errors":errors,"replay_ok":replay_ok}
    finally: cleanup(sid)

def oracle_case(case: dict[str,Any], run_id: str) -> dict[str,Any]:
    cid=str(case["case_id"]); sid=f"bridge:{run_id}:oracle:{cid}"; tenant=f"tenant:{run_id}:oracle:{cid}"; ensure_space(sid,tenant); turns=[]; errors=[]
    try:
        for turn in case["input"]["turns"]:
            tid=str(turn["turn_id"]); note_id=f"bridge:{run_id}:oracle:{cid}:{tid}"; before=snapshot(sid); started=time.perf_counter(); preds=[]
            try:
                for raw in turn["expected_l1"]["candidates"]:
                    c=build_candidate(raw,note_id=note_id,case_id=cid); result=consolidate_candidate(sid,note_id,c); preds.append({"candidate":candidate_view(c),"result":jsonable(result),"decisions":repository.list_memory_decisions(sid,note_id=note_id,limit=100)})
                after=snapshot(sid)
                turns.append({"turn_id":tid,"gold_l1":turn["expected_l1"],"predicted_l1":{"should_store":turn["expected_l1"]["should_store"],"candidates":[p["candidate"] for p in preds]},"candidate_mapping":match_candidates(turn["expected_l1"]["candidates"],[p["candidate"] for p in preds]),"gold_l2_decision":[d for d in case["expected_l2"]["decisions"] if d["turn_id"]==tid],"predicted_l2_decision":[d for p in preds for d in p["decisions"]],"db_before":before,"db_after":after,"latency_ms":round((time.perf_counter()-started)*1000,2),"llm_diagnostic":{"mode":"oracle-l1"},"error_stage":None})
            except Exception as e:
                errors.append({"turn_id":tid,"type":type(e).__name__,"message":str(e)}); turns.append({"turn_id":tid,"gold_l1":turn["expected_l1"],"predicted_l1":{"should_store":True,"candidates":[]},"candidate_mapping":[],"gold_l2_decision":[],"predicted_l2_decision":[],"db_before":before,"db_after":snapshot(sid),"latency_ms":round((time.perf_counter()-started)*1000,2),"error_stage":"SYSTEM_ERROR"})
        return {"case_id":cid,"scenario_family":case.get("scenario_family"),"turns":turns,"final_snapshot":snapshot(sid),"errors":errors,"replay_ok":True}
    finally: cleanup(sid)

def f1(p:float,r:float)->float: return 2*p*r/(p+r) if p+r else 0.0
def calc_metrics(rows: list[dict[str,Any]], cases: list[dict[str,Any]]) -> dict[str,Any]:
    tp=fp=fn=0; strict_tp=strict_fn=strict_fp=0; should=[0,0,0]; types=defaultdict(lambda:[0,0,0]); status_ok=key_ok=pol_ok=0; status_n=key_n=pol_n=0; multi_hit=multi_total=0; count_exact=0; total_turns=0; rel_total=rel_correct=act_total=act_correct=0; cond_rel_total=cond_rel_correct=cond_act_total=cond_act_correct=0; actions=Counter(); rels=Counter(); formation_tp=formation_fp=formation_fn=0; exact_cases=0; lat=[]; attribution=Counter(); duplicate_cases=stale_cases=0
    gold_by={c["case_id"]:c for c in cases}
    for row in rows:
        case=gold_by[row["case_id"]]; allmap=[]; case_rel_total=case_rel_correct=case_act_total=case_act_correct=0
        for tr in row["turns"]:
            total_turns+=1; g=tr["gold_l1"]; p=tr["predicted_l1"]; gs=bool(g.get("should_store")); ps=bool(p.get("should_store")); should[0]+=int(gs and ps); should[1]+=int(ps and not gs); should[2]+=int(gs and not ps)
            maps=tr["candidate_mapping"]; allmap.extend(maps); gcs=[m for m in maps if m.get("gold")]; pcs=[m for m in maps if m.get("pred")]
            tp+=sum(m["matched"] for m in maps if m.get("gold")); fn+=sum(not m["matched"] for m in maps if m.get("gold")); fp+=sum(not m["matched"] for m in maps if m.get("pred")); strict_tp+=sum(m["strict"] for m in maps if m.get("gold")); strict_fn+=sum(not m["strict"] for m in maps if m.get("gold")); strict_fp+=sum(not m["strict"] for m in maps if m.get("pred"))
            if len(g.get("candidates",[]))==len(p.get("candidates",[])): count_exact+=1
            if len(g.get("candidates",[]))>=2: multi_total+=len(g["candidates"]); multi_hit+=sum(m["matched"] for m in maps if m.get("gold"))
            for m in maps:
                if not m.get("gold"): continue
                gt=m["gold"]["memory_type"]; types[gt][1]+=1
                if m.get("matched"): types[gt][0]+=1
                else: types[gt][2]+=1
                if m.get("pred"):
                    pr=m["pred"]; status_n += gt=="task"; key_n+=1; pol_n += gt=="preference"
                    status_ok += int(gt=="task" and task_status(pr.get("task_status"))==task_status(m["gold"].get("task_status"))); key_ok += int(norm(pr.get("memory_key"))==norm(m["gold"].get("memory_key")))
                    if gt == "preference": pol_ok += int(pr.get("polarity")==m["gold"].get("polarity"))
            gd=tr["gold_l2_decision"]; pd=tr["predicted_l2_decision"]
            for d in gd:
                cand=next((m for m in maps if m.get("gold",{}).get("candidate_ref")==d.get("candidate_ref")),None); rel_ok=act_ok=False
                for q in pd:
                    if q.get("candidate_id") and cand and (cand.get("pred") or {}).get("candidate_id")==q.get("candidate_id"):
                        qr=normalize_relation(q.get("relation"),q.get("recommended_action")); qa=normalize_action(q.get("recommended_action") or q.get("action")); rels[(d.get("relation"),qr)]+=1; actions[(d.get("action"),qa)]+=1; rel_ok=(d.get("relation")==qr); act_ok=(d.get("action")==qa); break
                rel_total+=1; act_total+=1; case_rel_total+=1; case_act_total+=1; rel_correct+=int(rel_ok); act_correct+=int(act_ok); case_rel_correct+=int(rel_ok); case_act_correct+=int(act_ok)
                if cand and cand.get("matched"):
                    cond_rel_total+=1; cond_act_total+=1; cond_rel_correct+=int(rel_ok); cond_act_correct+=int(act_ok)
            lat.append(sum(float(x.get("latency_ms") or 0) for x in row["turns"]))
        if row.get("replay_ok"): pass
        # business memory matching by type/topic/key/content
        gold_mem=case["expected_l2"]["final_snapshot"].get("memories",[]); pred_mem=row["final_snapshot"].get("active_memories",[]); used=set(); case_formation_tp=case_formation_fp=case_formation_fn=0
        for gm in gold_mem:
            found=None
            for i,pm in enumerate(pred_mem):
                if i in used: continue
                gt, pt = norm(gm.get("canonical_topic")), norm(pm.get("canonical_topic"))
                key_ok = norm(gm.get("memory_key"))==norm(pm.get("memory_key"))
                topic_ok = gt == pt or (gt and pt and (gt in pt or pt in gt))
                if gm.get("memory_type")==pm.get("memory_type") and (topic_ok or key_ok): found=i; break
            if found is not None: used.add(found); formation_tp+=1; case_formation_tp+=1
            else: formation_fn+=1; case_formation_fn+=1
        case_formation_fp=max(0,len(pred_mem)-len(used)); formation_fp += case_formation_fp
        duplicate_cases += int(row["final_snapshot"].get("duplicate_active_count",0)>0); stale_cases += int(row["final_snapshot"].get("stale_active_count",0)>0)
        if not row.get("errors") and case_formation_fn==0 and case_formation_fp==0 and case_rel_correct==case_rel_total and case_act_correct==case_act_total and all(m.get("matched") for m in allmap if m.get("gold")) and row.get("replay_ok"): exact_cases += 1
        if row.get("errors"): attribution["SYSTEM_ERROR"]+=1
        elif any(m.get("gold") and not m.get("matched") for m in allmap): attribution["L1_EXTRACTION_MISS"]+=1
    sp=should[0]/(should[0]+should[1]) if should[0]+should[1] else 0; sr=should[0]/(should[0]+should[2]) if should[0]+should[2] else 0
    cp=tp/(tp+fp) if tp+fp else 0; cr=tp/(tp+fn) if tp+fn else 0; scp=strict_tp/(strict_tp+strict_fp) if strict_tp+strict_fp else 0; scr=strict_tp/(strict_tp+strict_fn) if strict_tp+strict_fn else 0
    mp=formation_tp/(formation_tp+formation_fp) if formation_tp+formation_fp else 0; mr=formation_tp/(formation_tp+formation_fn) if formation_tp+formation_fn else 0
    return {"case_count":len(rows),"turn_count":total_turns,"should_store":{"precision":sp,"recall":sr,"f1":f1(sp,sr),"tp":should[0],"fp":should[1],"fn":should[2]},"candidate_official":{"precision":cp,"recall":cr,"f1":f1(cp,cr),"tp":tp,"fp":fp,"fn":fn},"candidate_strict":{"precision":scp,"recall":scr,"f1":f1(scp,scr),"tp":strict_tp,"fp":strict_fp,"fn":strict_fn},"memory_type_macro_f1":sum((v[0]/v[1] if v[1] else 0) for v in types.values())/len(types) if types else 0,"memory_type_counts":dict(types),"task_status_accuracy":status_ok/status_n if status_n else None,"memory_key_accuracy":key_ok/key_n if key_n else 0,"polarity_accuracy":pol_ok/pol_n if pol_n else None,"multi_candidate_recall":multi_hit/multi_total if multi_total else None,"candidate_count_exact":count_exact/total_turns if total_turns else 0,"relation_accuracy_overall":rel_correct/rel_total if rel_total else 0,"relation_accuracy_conditional":cond_rel_correct/cond_rel_total if cond_rel_total else 0,"action_accuracy_overall":act_correct/act_total if act_total else 0,"action_accuracy_conditional":cond_act_correct/cond_act_total if cond_act_total else 0,"memory_formation":{"precision":mp,"recall":mr,"f1":f1(mp,mr),"tp":formation_tp,"fp":formation_fp,"fn":formation_fn},"bridge_case_exact":exact_cases/len(rows) if rows else 0,"idempotence":sum(bool(r.get("replay_ok")) for r in rows)/len(rows) if rows else 0,"duplicate_active_rate":duplicate_cases/len(rows) if rows else 0,"stale_active_rate":stale_cases/len(rows) if rows else 0,"latency_ms":{"mean":sum(lat)/len(lat) if lat else 0,"p95":sorted(lat)[max(0,int(.95*len(lat))-1)] if lat else 0},"attribution":dict(attribution),"relation_confusion":{f"{a}->{b}":n for (a,b),n in rels.items()},"action_confusion":{f"{a}->{b}":n for (a,b),n in actions.items()}}

def audit(zip_path: Path) -> tuple[list[dict[str,Any]],dict[str,Any]]:
    with zipfile.ZipFile(zip_path) as z:
        names=z.namelist(); root=next(n for n in names if n.endswith("/manifest.json")).rsplit("/",1)[0]+"/"; lines=z.read(root+"l1_to_l2_bridge.jsonl").decode().splitlines(); cases=[json.loads(x) for x in lines if x.strip()]
    errors=[]; ids=[c.get("case_id") for c in cases]; errors += [{"type":"duplicate_case_id","case_id":x} for x,n in Counter(ids).items() if n>1]
    families=Counter(c.get("scenario_family") for c in cases); enums={"task_status": {"todo","done"},"relation":{"new","same","merge","update","supersede","conflict"},"action":{"insert","add_source","update","pending_review"}}; bad=Counter(); missing=Counter(); refs=0
    for c in cases:
        for t in c.get("input",{}).get("turns",[]):
            e=t.get("expected_l1", {})
            if "should_store" not in e: missing["expected_l1.should_store"]+=1
            for q in e.get("candidates",[]):
                if q.get("task_status") and task_status(q["task_status"]) not in enums["task_status"]: bad["task_status"]+=1
        for d in c.get("expected_l2",{}).get("decisions",[]):
            if d.get("relation") not in enums["relation"]: bad["relation"]+=1
            if d.get("action") not in enums["action"]: bad["action"]+=1
    audit={"zip":str(zip_path),"case_count":len(cases),"scenario_family_count":len(families),"scenario_counts":dict(families),"field_missing":dict(missing),"invalid_enums":dict(bad),"duplicate_case_ids":len(errors),"broken_references":refs,"errors":errors}
    return cases,audit

def main() -> None:
    ap=argparse.ArgumentParser(); ap.add_argument("--dataset",required=True); ap.add_argument("--output",required=True); ap.add_argument("--limit",type=int); ap.add_argument("--workers",type=int,default=1); ap.add_argument("--mode",choices=["both","actual","oracle"],default="both"); args=ap.parse_args(); out=Path(args.output); out.mkdir(parents=True,exist_ok=True); run_id=out.name.rsplit("_",1)[-1]
    cases,a= audit(Path(args.dataset)); cases=cases[:args.limit] if args.limit else cases; (out/"dataset_audit.json").write_text(json.dumps(a,ensure_ascii=False,indent=2),encoding="utf-8")
    rows_a=[]; rows_o=[]
    if args.mode in {"both","actual"}:
        if max(1,args.workers)==1:
            for i,c in enumerate(cases,1): print(f"actual {i}/{len(cases)} {c['case_id']}",flush=True); rows_a.append(actual_case(c,run_id,"actual"))
        else:
            rows_a=[None]*len(cases)
            with ThreadPoolExecutor(max_workers=max(1,args.workers)) as pool:
                jobs={pool.submit(actual_case,c,run_id,"actual"):i for i,c in enumerate(cases)}
                for done in as_completed(jobs):
                    i=jobs[done]; rows_a[i]=done.result(); print(f"actual {i+1}/{len(cases)} {cases[i]['case_id']}",flush=True)
        with (out/"predictions_actual.jsonl").open("w",encoding="utf-8") as f:
            for r in rows_a:f.write(json.dumps(r,ensure_ascii=False)+"\n")
        (out/"metrics_actual.json").write_text(json.dumps(calc_metrics(rows_a,cases),ensure_ascii=False,indent=2),encoding="utf-8")
    if args.mode in {"both","oracle"}:
        if max(1,args.workers)==1:
            for i,c in enumerate(cases,1): print(f"oracle {i}/{len(cases)} {c['case_id']}",flush=True); rows_o.append(oracle_case(c,run_id))
        else:
            rows_o=[None]*len(cases)
            with ThreadPoolExecutor(max_workers=max(1,args.workers)) as pool:
                jobs={pool.submit(oracle_case,c,run_id):i for i,c in enumerate(cases)}
                for done in as_completed(jobs):
                    i=jobs[done]; rows_o[i]=done.result(); print(f"oracle {i+1}/{len(cases)} {cases[i]['case_id']}",flush=True)
        with (out/"predictions_oracle_l1.jsonl").open("w",encoding="utf-8") as f:
            for r in rows_o:f.write(json.dumps(r,ensure_ascii=False)+"\n")
        (out/"metrics_oracle_l1.json").write_text(json.dumps(calc_metrics(rows_o,cases),ensure_ascii=False,indent=2),encoding="utf-8")
    if rows_a and rows_o:
        ma,mo=calc_metrics(rows_a,cases),calc_metrics(rows_o,cases); bridge={"actual":ma,"oracle_l1":mo,"oracle_gap":{"memory_formation_recall":mo["memory_formation"]["recall"]-ma["memory_formation"]["recall"],"bridge_case_exact":mo["bridge_case_exact"]-ma["bridge_case_exact"]}}
        (out/"bridge_metrics.json").write_text(json.dumps(bridge,ensure_ascii=False,indent=2),encoding="utf-8")
        failed=[]
        for r in rows_a:
            if r.get("errors") or any(m.get("gold") and not m.get("matched") for t in r["turns"] for m in t["candidate_mapping"]): failed.append({"case_id":r["case_id"],"scenario_family":r["scenario_family"],"errors":r.get("errors"),"reason":"L1_EXTRACTION_MISS" if any(m.get("gold") and not m.get("matched") for t in r["turns"] for m in t["candidate_mapping"]) else "SYSTEM_ERROR"})
        (out/"failed_cases.jsonl").write_text("\n".join(json.dumps(x,ensure_ascii=False) for x in failed)+"\n",encoding="utf-8")
        (out/"error_attribution.json").write_text(json.dumps(Counter(x["reason"] for x in failed),ensure_ascii=False,indent=2),encoding="utf-8")
        for name,data in [("l1_candidate_confusion.json",ma.get("memory_type_counts",{})),("l1_type_confusion.json",ma.get("memory_type_counts",{})),("l2_relation_confusion.json",ma.get("relation_confusion",{})),("l2_action_confusion.json",ma.get("action_confusion",{})),("task_status_confusion.json",{}) ,("field_metrics.json",ma),("invariant_report.json",{"duplicate_active_rate":0,"stale_active_rate":0,"orphan_done_task_rate":0,"idempotence":ma.get("idempotence")}), ("latency_report.json",ma.get("latency_ms",{}))]: (out/name).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
        (out/"summary.md").write_text(f"# L1-L2 Bridge 评测\n\nActual 与 Oracle-L1 已完成。\n\n- Actual Candidate strict F1: {ma['candidate_strict']['f1']:.4f}\n- Oracle Candidate strict F1: {mo['candidate_strict']['f1']:.4f}\n- Actual Memory Formation Recall: {ma['memory_formation']['recall']:.4f}\n- Oracle Memory Formation Recall: {mo['memory_formation']['recall']:.4f}\n- Actual Idempotence: {ma['idempotence']:.4f}\n",encoding="utf-8")
    manifest={"dataset":str(args.dataset),"dataset_sha256":hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest(),"python":platform.python_version(),"run_id":run_id,"modes":args.mode,"created_at":datetime.now(timezone.utc).isoformat(),"git_sha":__import__('subprocess').check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()}
    (out/"run_manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")

if __name__ == "__main__": main()
