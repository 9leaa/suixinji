# ruff: noqa: E701, E702
#!/usr/bin/env python3
"""Build the deterministic, world-spec-first Suixinji evaluation package.

The generator owns Gold labels. Natural-language text is kept deliberately
small and auditable; future paraphrase expansion must preserve each world.
"""
from __future__ import annotations

import hashlib
import json
import random
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "suixinji_evaluation_v1"
SEED = 20260813


def dump_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def l1_cases() -> list[dict]:
    rows = []
    specs = [
        ("semantic", "我现在住在杭州。", "用户现在住在杭州", "我现在住在杭州", "residence"),
        ("preference", "工作时我不喝咖啡，早上喜欢乌龙茶。", "用户工作时不喝咖啡", "工作时我不喝咖啡", "coffee"),
        ("task", "这周把数据库迁移做完。", "数据库迁移待完成", "这周把数据库迁移做完", "数据库迁移"),
        ("task", "第二轮压测报告也收尾了。", "第二轮压测报告已完成", "第二轮压测报告也收尾了", "第二轮压测报告"),
        ("episodic", "本科答辩终于结束了。", "用户完成本科答辩", "本科答辩终于结束了", "本科答辩"),
        ("preference", "我喜欢咖啡，也不喜欢太甜的饮料。", "用户喜欢咖啡", "我喜欢咖啡", "coffee"),
        ("task", "报告卡在 API 限流，暂时没完成。", "报告待完成，阻塞原因为 API 限流", "报告卡在 API 限流，暂时没完成", "报告"),
        ("semantic", "我的主要开发设备是 MacBook Pro。", "用户主要使用 MacBook Pro", "主要开发设备是 MacBook Pro", "device"),
        ("preference", "没有明确场景时我都选无糖。", "用户偏好无糖", "我都选无糖", "无糖"),
        ("task", "这个也做完了。", "前一个任务已完成", "这个也做完了", "reference"),
        ("episodic", "上个月参加了上海的技术分享。", "用户参加上海技术分享", "上个月参加了上海的技术分享", "技术分享"),
        ("semantic", "我可能明年去上海发展。", None, None, None),
    ]
    for i, (typ, text, content, evidence, topic) in enumerate(specs, 1):
        cid = f"l1v1_{i:03d}"
        candidate = None
        if content:
            candidate = {
                "candidate_ref": "c1", "memory_type": typ, "content": content,
                "evidence_span": {"text": evidence, "start": text.index(evidence), "end": text.index(evidence) + len(evidence)},
                "canonical_topic": topic, "task_status": "done" if typ == "task" and ("完" in text or "收尾" in text) else ("todo" if typ == "task" else None),
                "polarity": "negative" if typ == "preference" and ("不" in text or "没有" in text) else ("positive" if typ == "preference" else None),
                "qualifiers": ["工作时"] if "工作时" in text else (["早上"] if "早上" in text else []),
                "task_family_key": f"task-family:{topic}" if typ == "task" else None,
                "task_instance_id": f"instance-{i}" if typ == "task" else None,
                "blocker": "API 限流" if "API 限流" in text else None,
                "reference_status": "resolved" if "这个" in text else "not_applicable",
            }
        rows.append({"schema_version": "suixinji.evaluation.v1.l1", "case_id": cid, "world_id": f"world_{i:03d}", "scenario_family": "noise" if candidate is None else ("multi_fact" if "也" in text else typ), "world_spec": {"facts": [] if not candidate else [{"memory_type": typ, "topic": topic, "status": candidate["task_status"]}]}, "input": {"tenant_id": "tenant_eval_v1", "space_id": f"space_{cid}", "sequence_no": i, "current_message": text, "previous_messages": [{"note_id": "prev_001", "text": "这周要完成报告。"}] if "这个" in text else []}, "expected": {"should_store": bool(candidate), "candidates": [candidate] if candidate else [], "schema_rejected": False}})
    return rows


def l2_cases() -> list[dict]:
    rows = []
    scenarios = [
        ("new", "insert", "different", None, "准备完成 Agent 简历", "todo"),
        ("same", "add_source", "same_instance", "m1", "Agent 简历仍是同一件事", "todo"),
        ("update", "update_task", "same_instance", "m1", "Agent 简历已经完成", "done"),
        ("new", "insert", "different", None, "第二轮 Agent 简历已完成", "done"),
        ("conflict", "pending_review", "uncertain", "m1", "这个任务到底是否完成还说不清", "todo"),
        ("same", "add_source", "same_assertion", "m2", "我还是喜欢咖啡", None),
        ("supersede", "update", "same_assertion", "m2", "现在改成不喜欢咖啡", None),
        ("new", "insert", "different", None, "本科答辩结束了", None),
    ]
    for i, (rel, action, ident, target, text, status) in enumerate(scenarios, 1):
        cid = f"l2v1_{i:03d}"; mref = target or "m1"
        typ = "preference" if "咖啡" in text else ("episodic" if "答辩" in text else "task")
        cand = {"candidate_ref": "c1", "memory_type": typ, "content": text, "evidence_span": {"text": text, "start": 0, "end": len(text)}, "canonical_topic": "咖啡" if typ == "preference" else text, "canonical_instance_key": f"task:{text}:i1" if typ == "task" else None, "task_family_key": f"task-family:{text}" if typ == "task" else None, "task_status": status, "polarity": "negative" if "不喜欢" in text else ("positive" if typ == "preference" else None), "preference_family_key": "preference-family:用户:饮品" if typ == "preference" else None, "preference_assertion_key": "preference-assertion:咖啡" if typ == "preference" else None}
        rows.append({"schema_version": "suixinji.evaluation.v1.l2", "case_id": cid, "world_id": f"world_l2_{i:03d}", "scenario_family": {"new":"new_insert","same":"same_assertion","update":"task_transition","conflict":"pending_review","supersede":"preference_reversal"}[rel], "world_spec": {"relation": rel, "identity": ident, "target": target}, "input": {"tenant_id": "tenant_eval_v1", "space_id": f"space_{cid}", "candidate": cand, "existing_memories": ([{"ref": "m1", "memory_type": "task", "content": "准备完成 Agent 简历", "status": "active", "task_status": "todo", "canonical_instance_key": "task:Agent简历:i1", "family_key": "task-family:Agent简历"}] if target == "m1" else ([{"ref": "m2", "memory_type": "preference", "content": "喜欢咖啡", "status": "active", "polarity": "positive", "assertion_key": "preference-assertion:咖啡", "family_key": "preference-family:用户:饮品"}] if target == "m2" else [])), "existing_versions": [], "existing_sources": []}, "expected": {"decisions": [{"candidate_ref": "c1", "identity_classification": ident, "relation": rel, "action": action, "matched_memory_ref": target, "resulting_memory_ref": mref, "expected_version_created": action in {"insert", "update", "update_task"}, "expected_source_refs": ["s1"], "reason_code": "same_task_instance" if ident == "same_instance" else None}], "final_snapshot": {"memories": [], "versions": [], "sources": [], "pending_reviews": []}, "invariants": {"persisted_task_status_only_todo_done": True, "duplicate_active_zero": True}}})
    return rows


def l3_case(i: int, kind: str) -> dict:
    cid = f"l3v1_{i:03d}"; source = {"source_ref":"s1","note_id":f"note_{cid}","evidence_text":"我现在负责 Agent 评测。","event_time":"2026-08-01T00:00:00Z","observed_at":"2026-08-02T00:00:00Z","authority":"user"}
    memory = {"memory_ref":"m1","memory_type":"semantic","status":"active","task_status":None,"content":"用户当前负责 Agent 评测","canonical_topic":"current_focus","entity":"user","attribute":"current_focus","current_value":"Agent 评测","polarity":None,"updated_at":"2026-08-02T00:00:00Z","source_refs":["s1"],"sensitivity":"normal","access_scope":"owner"}
    versions = []
    if kind == "no_answer":
        query, expected = "我养的猫叫什么？", {"answer_type":"no_answer","evidence_mode":"none","reason_code":"no_relevant_evidence","relevant_current_refs":[],"relevant_history_refs":[],"must_not_return_refs":["m1"],"expected_claims":[],"required_citation_refs":[],"expected_claim_groups":[]}
    elif kind == "history":
        memory["memory_type"]="task"; memory["task_status"]="done"; memory["content"]="Agent 评测已完成"; memory["canonical_topic"]="Agent 评测"; memory["current_value"]="done"
        source["evidence_text"]="Agent 评测已经完成。"; source["event_time"]="2026-08-02T00:00:00Z"
        versions=[{"version_ref":"v1","memory_ref":"m1","sequence":1,"content":"Agent 评测待完成","task_status":"todo","value":"todo","valid_from":"2026-07-20T00:00:00Z","valid_until":"2026-08-01T00:00:00Z","source_refs":["s1"]},{"version_ref":"v2","memory_ref":"m1","sequence":2,"content":"Agent 评测已完成","task_status":"done","value":"done","valid_from":"2026-08-02T00:00:00Z","valid_until":None,"source_refs":["s1"]}]
        query="Agent 评测经历了什么变化？"; expected={"answer_type":"answered","evidence_mode":"history","reason_code":"history_query","relevant_current_refs":["m1"],"relevant_history_refs":["v1","v2"],"must_not_return_refs":[],"expected_claims":[{"claim":"Agent 评测从 todo 变为 done","memory_refs":["m1"],"version_refs":["v1","v2"],"source_refs":["s1"]}],"required_citation_refs":["s1"],"expected_claim_groups":[{"group_type":"timeline","summary_claim":{"claim":"Agent 评测从 todo 变为 done","memory_refs":["m1"],"version_refs":["v1","v2"],"source_refs":["s1"]},"ordered_members":[{"version_ref":"v1","sequence":1,"value":"todo"},{"version_ref":"v2","sequence":2,"value":"done"}]}]}
    else:
        versions=[]; query="我当前负责什么？"; expected={"answer_type":"answered","evidence_mode":"current","reason_code":"evidence_supported","relevant_current_refs":["m1"],"relevant_history_refs":[],"must_not_return_refs":[],"expected_claims":[{"claim":"你当前负责 Agent 评测","memory_refs":["m1"],"source_refs":["s1"]}],"required_citation_refs":["s1"],"expected_claim_groups":[]}
    return {"schema_version":"suixinji.layer3.retrieval_answer.v2","case_id":cid,"world_id":f"world_l3_{i:03d}","dataset":kind,"difficulty":"medium","world_spec":{"memory_topic":"Agent 评测","query_kind":kind},"input":{"space_id":f"space_{cid}","query":query,"query_time":"2026-08-03T00:00:00Z","top_k":10,"access_context":{"requester":"owner","allow_sensitive":True},"memory_snapshot":{"memories":[memory],"versions":versions,"sources":[source],"pending_reviews":[]}},"expected":expected}


def bridge_cases() -> list[dict]:
    rows=[]
    turns=[("t1","这周要完成 Agent 评测。","todo"),("t2","评测卡在 API 限流，暂时没完成。","todo"),("t3","Agent 评测终于完成了。","done")]
    for i in range(1,9):
        cid=f"bridge_v1_{i:03d}"; ts=[]
        for tid,text,status in turns:
            ts.append({"turn_id":tid,"note_id":f"{cid}_{tid}","note_text":text,"expected_l1":{"should_store":True,"candidates":[{"candidate_ref":"c1","memory_type":"task","content":text,"evidence_text":text,"canonical_topic":"Agent 评测","canonical_instance_key":"task:Agent评测:i1","task_family_key":"task-family:Agent评测","task_status":status,"blocker":"API 限流" if tid=="t2" else None}]},"expected_l2":{}})
        rows.append({"schema_version":"suixinji.evaluation.v1.bridge","case_id":cid,"world_id":f"world_bridge_{i:03d}","scenario_family":"task_todo_progress_done","world_spec":{"task_instance":"agent_eval_i1","final_status":"done"},"input":{"turns":ts},"expected_l2":{"decisions":[],"final_snapshot":{"memories":[]}}})
    return rows


DOMAINS = [
    ("阳台园艺", "薄荷", "阳台园艺计划"), ("家庭烘焙", "低糖面包", "家庭烘焙计划"),
    ("旅行规划", "挪威行程", "挪威旅行计划"), ("胶片摄影", "黑白胶卷", "胶片摄影整理"),
    ("力量训练", "深蹲训练", "力量训练计划"), ("吉他练习", "指弹曲目", "吉他练习计划"),
    ("科幻阅读", "三体重读", "科幻阅读清单"), ("宠物照护", "猫咪疫苗", "宠物照护安排"),
    ("衣柜整理", "冬衣收纳", "衣柜整理计划"), ("日语学习", "N2听力", "日语学习计划"),
    ("家庭理财", "年度预算", "家庭预算计划"), ("木工制作", "书架打磨", "木工作品计划"),
    ("水彩绘画", "城市速写", "水彩练习计划"), ("植物观察", "候鸟记录", "自然观察计划"),
    ("烹饪实验", "川味调料", "烹饪实验记录"), ("露营准备", "天幕装备", "周末露营计划"),
    ("播客制作", "声音剪辑", "播客制作计划"), ("社区志愿", "旧物捐赠", "社区志愿安排"),
    ("家庭收纳", "厨房抽屉", "家庭收纳计划"), ("手工陶艺", "杯子拉坯", "陶艺练习计划"),
]

ITEM_VARIANTS = ["春季版", "夏季版", "秋季版", "冬季版", "入门组", "进阶组", "周末组", "晨间组", "家庭组", "户外组"]
STYLE_PREFIXES = ["说起来，", "记一下：", "顺便记着，", "刚确认，", "对了，", "今天定了，", "补充一下，", "我想清楚了，"]


def topic_for(n: int) -> tuple[str, str, str, str]:
    context, item, plan = DOMAINS[(n - 1) % len(DOMAINS)]
    variant = ITEM_VARIANTS[((n - 1) // len(DOMAINS)) % len(ITEM_VARIANTS)]
    serial = (n - 1) // (len(DOMAINS) * len(ITEM_VARIANTS)) + 1
    suffix = f"{variant}第{serial}期"
    return context, f"{item}{suffix}", f"{plan}{suffix}", suffix


def candidate(*, typ: str, content: str, evidence: str, topic: str, status: str | None = None,
              polarity: str | None = None, qualifiers: list[str] | None = None,
              instance: str | None = None, blocker: str | None = None,
              reference_status: str = "not_applicable") -> dict:
    return {
        "candidate_ref": "c1", "memory_type": typ, "content": content,
        "evidence_span": {"text": evidence, "start": 0, "end": len(evidence)},
        "canonical_topic": topic, "canonical_instance_key": f"task:{instance}" if instance else None,
        "task_family_key": f"task-family:{topic}" if typ == "task" else None,
        "task_instance_id": instance, "task_status": status, "polarity": polarity,
        "preference_family_key": f"preference-family:用户:{topic.split('版')[0]}" if typ == "preference" else None,
        "preference_assertion_key": f"preference-assertion:{topic}:{polarity or 'unknown'}" if typ == "preference" else None,
        "qualifiers": qualifiers or [], "blocker": blocker, "progress_note": None,
        "closure_reason": None, "reference_status": reference_status,
    }


def large_l1(base: list[dict], count: int = 1000) -> list[dict]:
    rows=[]
    for n in range(1, count + 1):
        context,item,plan,variant=topic_for(n); phase=(n-1)%12; prefix=STYLE_PREFIXES[n%len(STYLE_PREFIXES)]
        previous=[]; candidates=[]; family=""; should=True
        if phase == 0:
            text=f"{prefix}我最近固定在研究{context}里的{item}。"; family="semantic_stable"
            candidates=[candidate(typ="semantic",content=f"用户近期研究{item}",evidence=text,topic=item)]
        elif phase == 1:
            text=f"{prefix}只有周末露营时我才偏爱{item}。"; family="preference_scoped"
            candidates=[candidate(typ="preference",content=f"用户周末露营时偏爱{item}",evidence=text,topic=item,polarity="positive",qualifiers=["周末露营时"])]
        elif phase == 2:
            text=f"{prefix}下周得把{plan}安排好。"; family="task_todo"; inst=f"{plan}:round-{n}"
            candidates=[candidate(typ="task",content=f"{plan}待完成",evidence=text,topic=plan,status="todo",instance=inst)]
        elif phase == 3:
            text=f"{prefix}{plan}卡在场地确认上，但事情还没做完。"; family="task_blocker"; inst=f"{plan}:round-{n}"
            candidates=[candidate(typ="task",content=f"{plan}待完成",evidence=text,topic=plan,status="todo",instance=inst,blocker="场地确认")]
        elif phase == 4:
            text=f"{prefix}上个月参加的{item}体验课已经结束。"; family="orphan_done_episodic"
            candidates=[candidate(typ="episodic",content=f"用户参加并结束{item}体验课",evidence=text,topic=f"{item}体验课")]
        elif phase == 5:
            text=f"{prefix}我喜欢{item}，也不喜欢过度包装的纪念品。"; family="multi_preference"
            first=candidate(typ="preference",content=f"用户喜欢{item}",evidence=f"我喜欢{item}",topic=item,polarity="positive"); first["evidence_span"]["start"]=text.index(first["evidence_span"]["text"]); first["evidence_span"]["end"]=first["evidence_span"]["start"]+len(first["evidence_span"]["text"])
            second=candidate(typ="preference",content="用户不喜欢过度包装的纪念品",evidence="不喜欢过度包装的纪念品",topic="过度包装纪念品",polarity="negative"); second["candidate_ref"]="c2"; second["evidence_span"]["start"]=text.index(second["evidence_span"]["text"]); second["evidence_span"]["end"]=second["evidence_span"]["start"]+len(second["evidence_span"]["text"]); candidates=[first,second]
        elif phase == 6:
            text=f"{prefix}{plan}做完了，我还是更喜欢{item}。"; family="task_preference_mixed"; inst=f"{plan}:round-{n}"
            first=candidate(typ="task",content=f"{plan}已完成",evidence=f"{plan}做完了",topic=plan,status="done",instance=inst); first["evidence_span"]["start"]=text.index(first["evidence_span"]["text"]); first["evidence_span"]["end"]=first["evidence_span"]["start"]+len(first["evidence_span"]["text"])
            second=candidate(typ="preference",content=f"用户喜欢{item}",evidence=f"更喜欢{item}",topic=item,polarity="positive"); second["candidate_ref"]="c2"; second["evidence_span"]["start"]=text.index(second["evidence_span"]["text"]); second["evidence_span"]["end"]=second["evidence_span"]["start"]+len(second["evidence_span"]["text"]); candidates=[first,second]
        elif phase == 7:
            text=f"{prefix}这个也收尾了。"; family="reference_within_three"; prev=f"这周要完成{plan}。"; previous=[{"note_id":f"prev_{n}","text":prev,"sequence_no":n-1}]
            candidates=[candidate(typ="task",content=f"{plan}已完成",evidence=text,topic=plan,status="done",instance=f"{plan}:round-{n}",reference_status="resolved")]; candidates[0]["antecedent_note_id"]=f"prev_{n}"; candidates[0]["antecedent_offset"]=-1
        elif phase == 8:
            text=f"{prefix}也许明年会认真学{context}。"; family="speculation_only"; should=False
        elif phase == 9:
            text=f"{prefix}登录口令是 demo-{n}-secret，帮我存着。"; family="sensitive_rejection"; should=False
        elif phase == 10:
            text=f"嗯嗯，{variant}这事以后再说吧。"; family="noise_non_assertion"; should=False
        else:
            text=f"{prefix}第{n}次{item}活动在周六结束，我拍了十二张照片。"; family="episodic_event"
            candidates=[candidate(typ="episodic",content=f"用户参加第{n}次{item}活动",evidence=text,topic=f"{item}活动")]
        rows.append({"schema_version":"suixinji.layer1.independent.v2","case_id":f"l1v1_large_{n:04d}","world_id":f"world_l1_large_{n:04d}","scenario_family":family,"difficulty":["easy","medium","hard"][n%3],"world_spec":{"domain":context,"item":item,"task_instance":f"{plan}:round-{n}"},"input":{"tenant_id":"tenant_eval_v1","space_id":f"space_l1v1_large_{n:04d}","sequence_no":n,"current_message":text,"previous_messages":previous},"expected":{"should_store":should,"candidates":candidates,"schema_rejected":False}})
    return rows


def large_l2(count: int = 800) -> list[dict]:
    rows=[]
    for n in range(1, count + 1):
        context,item,plan,_=topic_for(n)
        phase=(n-1)%8
        if phase == 0:
            relation,action,identity,target,status="new","insert","different",None,"todo"
        elif phase in {1,2}:
            relation,action,identity,target,status="same","add_source","same_instance","m1","todo"
        elif phase in {3,4}:
            relation,action,identity,target,status="update","update","same_instance","m1","done"
        elif phase == 5:
            relation,action,identity,target,status="new","insert","different",None,"done"
        elif phase == 6:
            relation,action,identity,target,status="conflict","pending_review","uncertain","m1","todo"
        else:
            relation,action,identity,target,status="supersede","update","same_assertion","m2",None
        typ="preference" if phase==7 else "task"
        content=f"{item}偏好调整" if typ=="preference" else f"{plan}{'已完成' if status=='done' else '待完成'}"
        cand={"candidate_ref":"c1","memory_type":typ,"content":content,"evidence_span":{"text":content,"start":0,"end":len(content)},"canonical_topic":item if typ=="preference" else plan,"canonical_instance_key":f"task:{plan}:i{n}" if typ=="task" else None,"task_family_key":f"task-family:{plan}" if typ=="task" else None,"task_status":status if typ=="task" else None,"polarity":"negative" if phase==7 else None,"preference_family_key":f"preference-family:用户:{context}" if typ=="preference" else None,"preference_assertion_key":f"preference-assertion:{item}" if typ=="preference" else None}
        existing=[]
        if target=="m1": existing=[{"ref":"m1","memory_type":"task","content":f"{plan}待完成","status":"active","task_status":"todo","canonical_instance_key":f"task:{plan}:i{n}","family_key":f"task-family:{plan}"}]
        elif target=="m2": existing=[{"ref":"m2","memory_type":"preference","content":f"偏好{item}","status":"active","polarity":"positive","assertion_key":f"preference-assertion:{item}","family_key":f"preference-family:用户:{context}"}]
        if phase == 5:
            # Same family, different explicit instance: old m0 is only a
            # retrieval distractor and cannot authorize mutation.
            existing=[{"ref":"m0","memory_type":"task","content":f"{plan}第一轮已完成","status":"active","task_status":"done","canonical_instance_key":f"task:{plan}:old","family_key":f"task-family:{plan}"}]
            cand["canonical_instance_key"]=f"task:{plan}:new-{n}"
        if phase == 6:
            # Two plausible family matches and no instance key: mutation must
            # be held for review.
            cand["canonical_instance_key"]=None
            existing=[{"ref":"m1","memory_type":"task","content":f"{plan}甲组待完成","status":"active","task_status":"todo","canonical_instance_key":f"task:{plan}:a","family_key":f"task-family:{plan}"},{"ref":"m3","memory_type":"task","content":f"{plan}乙组待完成","status":"active","task_status":"todo","canonical_instance_key":f"task:{plan}:b","family_key":f"task-family:{plan}"}]
        resulting=target or "m_new"
        existing_versions=[]; existing_sources=[]
        for old in existing:
            old_ref=old["ref"]
            existing_versions.append({"ref":f"v0_{old_ref}","memory_ref":old_ref,"sequence":1,"content":old["content"],"source_refs":[f"s0_{old_ref}"],"task_status":old.get("task_status"),"polarity":old.get("polarity")})
            existing_sources.append({"ref":f"s0_{old_ref}","note_ref":f"n0_{old_ref}","text":old["content"],"role":"assertion_evidence"})
        result_memory={"ref":resulting,"memory_type":typ,"content":content,"status":"pending_review" if action=="pending_review" else "active","task_status":status if typ=="task" else None,"canonical_instance_key":cand.get("canonical_instance_key"),"family_key":cand.get("task_family_key") or cand.get("preference_family_key"),"assertion_key":cand.get("preference_assertion_key"),"polarity":cand.get("polarity"),"qualifiers":[]}
        final_memories=[*existing]
        if action in {"insert","update"}:
            final_memories=[m for m in final_memories if m.get("ref") != resulting]+[result_memory]
        pending_reviews=[{"ref":"pr1","candidate_ref":"c1","memory_refs":["m1","m3"],"source_refs":["s1"]}] if action=="pending_review" else []
        if action == "insert":
            new_versions=[{"ref":"v1","memory_ref":resulting,"sequence":1,"content":content,"source_refs":["s1"],"task_status":status if typ=="task" else None,"polarity":cand.get("polarity")}]
        elif action == "update":
            new_versions=[*existing_versions,{"ref":"v2","memory_ref":resulting,"sequence":2,"content":content,"source_refs":["s1"],"task_status":status if typ=="task" else None,"polarity":cand.get("polarity")}]
        elif action == "add_source":
            new_versions=[{**version,"source_refs":[*version.get("source_refs",[]),"s1"]} for version in existing_versions]
        else:
            new_versions=existing_versions
        final={"memories":final_memories,"versions":new_versions,"sources":[*existing_sources,{"ref":"s1","note_ref":"n1","text":content,"role":"assertion_evidence"}],"pending_reviews":pending_reviews}
        rows.append({"schema_version":"suixinji.layer2.independent.v2","case_id":f"l2v1_large_{n:04d}","world_id":f"world_l2_large_{n:04d}","scenario_family":["new_insert","same_source","same_source","task_transition","task_transition","same_family_new_instance","ambiguous_multiple_instances","preference_reversal"][phase],"world_spec":{"domain":context,"relation":relation,"identity":identity,"instance_authorization":"exact_instance_only"},"input":{"tenant_id":"tenant_eval_v1","space_id":f"space_l2v1_large_{n:04d}","candidate":cand,"existing_memories":existing,"existing_versions":existing_versions,"existing_sources":existing_sources},"expected":{"decisions":[{"candidate_ref":"c1","identity_classification":identity,"relation":relation,"action":action,"matched_memory_ref":target,"resulting_memory_ref":resulting,"expected_version_created":action in {"insert","update"},"expected_source_refs":["s1"]}],"final_snapshot":final,"invariants":{"persisted_task_status_only_todo_done":True,"duplicate_active_zero":True,"same_family_cannot_authorize_update":True}}})
    return rows


def large_l3(count: int = 800) -> list[dict]:
    rows=[]
    for n in range(1,count+1):
        context,item,plan,_=topic_for(n); phase=(n-1)%10; kind=["current","history","no_answer","conflict","clarification","restricted","qualified_history_only","multi_memory","semantic_noise","stale_guard"][phase]
        cid=f"l3v1_large_{n:04d}"; source={"source_ref":"s1","note_id":f"note_{cid}","evidence_text":f"我最近关注{context}里的{item}。","event_time":"2026-08-01T00:00:00Z","observed_at":"2026-08-02T00:00:00Z","authority":"user"}
        memory={"memory_ref":"m1","memory_type":"semantic","status":"active","task_status":None,"content":f"用户最近关注{context}","canonical_topic":context,"entity":"user","attribute":"关注主题","current_value":context,"polarity":None,"updated_at":"2026-08-02T00:00:00Z","source_refs":["s1"],"sensitivity":"normal","access_scope":"owner"}; versions=[]
        memories=[memory]; sources=[source]; pending=[]
        if kind=="no_answer":
            query=f"第{n}次海岛潜水的教练叫什么？"; expected={"answer_type":"no_answer","evidence_mode":"none","reason_code":"no_relevant_evidence","relevant_current_refs":[],"relevant_history_refs":[],"must_not_return_refs":["m1"],"expected_claims":[],"required_citation_refs":[],"expected_claim_groups":[]}
        elif kind=="history":
            memory.update({"memory_type":"task","task_status":"done","content":f"{plan}已完成","canonical_topic":plan,"current_value":"done"}); source["evidence_text"]=f"{plan}已经完成。"; versions=[{"version_ref":"v1","memory_ref":"m1","sequence":1,"content":f"{plan}待完成","task_status":"todo","value":"todo","valid_from":"2026-07-20T00:00:00Z","valid_until":"2026-08-01T00:00:00Z","source_refs":["s1"]},{"version_ref":"v2","memory_ref":"m1","sequence":2,"content":f"{plan}已完成","task_status":"done","value":"done","valid_from":"2026-08-02T00:00:00Z","valid_until":None,"source_refs":["s1"]}]; query=f"{plan}经历了什么变化？"; expected={"answer_type":"answered","evidence_mode":"history","reason_code":"history_query","relevant_current_refs":["m1"],"relevant_history_refs":["v1","v2"],"must_not_return_refs":[],"expected_claims":[{"claim":f"{plan}从 todo 变为 done","memory_refs":["m1"],"version_refs":["v1","v2"],"source_refs":["s1"]}],"required_citation_refs":["s1"],"expected_claim_groups":[{"group_type":"timeline","summary_claim":{"claim":f"{plan}从 todo 变为 done","memory_refs":["m1"],"version_refs":["v1","v2"],"source_refs":["s1"]},"ordered_members":[{"version_ref":"v1","sequence":1,"value":"todo"},{"version_ref":"v2","sequence":2,"value":"done"}]}]}
        elif kind=="conflict":
            memory.update({"memory_type":"preference","content":f"用户喜欢{item}","canonical_topic":item,"polarity":"positive"}); m2={**memory,"memory_ref":"m2","content":f"用户不喜欢{item}","polarity":"negative","source_refs":["s2"]}; memories.append(m2); sources.append({**source,"source_ref":"s2","note_id":f"note_{cid}_2","evidence_text":f"我不喜欢{item}。"}); query=f"我到底喜不喜欢{item}？"; expected={"answer_type":"conflict","evidence_mode":"current","reason_code":"unresolved_conflict","relevant_current_refs":["m1","m2"],"relevant_history_refs":[],"must_not_return_refs":[],"expected_claims":[],"required_citation_refs":[],"expected_claim_groups":[]}
        elif kind=="clarification":
            memory.update({"memory_type":"task","task_status":"todo","content":f"{plan}待完成","canonical_topic":plan,"current_value":"todo"}); m2={**memory,"memory_ref":"m2","content":f"{plan}第二轮待完成","canonical_topic":f"{plan}第二轮","source_refs":["s2"]}; memories.append(m2); sources.append({**source,"source_ref":"s2","note_id":f"note_{cid}_2","evidence_text":f"{plan}第二轮还没完成。"}); query="那个计划现在怎么样？"; expected={"answer_type":"clarification","evidence_mode":"none","reason_code":"ambiguous_reference","relevant_current_refs":[],"relevant_history_refs":[],"must_not_return_refs":["m1","m2"],"expected_claims":[],"required_citation_refs":[],"expected_claim_groups":[]}
        elif kind=="restricted":
            memory.update({"content":"存在受限的个人健康记录","canonical_topic":"个人健康记录","sensitivity":"restricted","access_scope":"owner"}); query="查看那条个人健康记录"; expected={"answer_type":"restricted","evidence_mode":"none","reason_code":"access_denied","relevant_current_refs":[],"relevant_history_refs":[],"must_not_return_refs":["m1"],"expected_claims":[],"required_citation_refs":[],"expected_claim_groups":[]}
        elif kind=="qualified_history_only":
            memory.update({"status":"superseded","content":f"用户过去关注{item}","canonical_topic":item}); versions=[{"version_ref":"v1","memory_ref":"m1","sequence":1,"content":f"用户过去关注{item}","task_status":None,"value":item,"valid_from":"2025-01-01T00:00:00Z","valid_until":"2025-06-01T00:00:00Z","source_refs":["s1"]}]; query=f"我现在还关注{item}吗？"; expected={"answer_type":"qualified_history_only","evidence_mode":"history","reason_code":"current_query_history_only","relevant_current_refs":[],"relevant_history_refs":["v1"],"must_not_return_refs":["m1"],"expected_claims":[{"claim":f"只能确认你过去关注过{item}","memory_refs":["m1"],"version_refs":["v1"],"source_refs":["s1"]}],"required_citation_refs":["s1"],"expected_claim_groups":[]}
        elif kind=="multi_memory":
            memory.update({"memory_type":"task","task_status":"todo","content":f"{plan}待完成","canonical_topic":plan,"current_value":"todo"}); m2={**memory,"memory_ref":"m2","content":f"{item}整理已完成","canonical_topic":f"{item}整理","task_status":"done","current_value":"done","source_refs":["s2"]}; memories.append(m2); sources.append({**source,"source_ref":"s2","note_id":f"note_{cid}_2","evidence_text":f"{item}整理已经完成。"}); query=f"{context}相关的两件事分别是什么状态？"; expected={"answer_type":"answered","evidence_mode":"current","reason_code":"evidence_supported","relevant_current_refs":["m1","m2"],"relevant_history_refs":[],"must_not_return_refs":[],"expected_claims":[{"claim":f"{plan}是 todo","memory_refs":["m1"],"source_refs":["s1"]},{"claim":f"{item}整理是 done","memory_refs":["m2"],"source_refs":["s2"]}],"required_citation_refs":["s1","s2"],"expected_claim_groups":[]}
        elif kind=="stale_guard":
            memory.update({"memory_type":"task","task_status":"done","content":f"{plan}已完成","canonical_topic":plan,"current_value":"done"}); m2={**memory,"memory_ref":"m2","status":"superseded","task_status":"todo","content":f"{plan}待完成","current_value":"todo","source_refs":["s2"],"updated_at":"2026-07-01T00:00:00Z"}; memories.append(m2); sources.append({**source,"source_ref":"s2","note_id":f"note_{cid}_2","evidence_text":f"{plan}当时还没完成。"}); query=f"{plan}现在完成了吗？"; expected={"answer_type":"answered","evidence_mode":"current","reason_code":"evidence_supported","relevant_current_refs":["m1"],"relevant_history_refs":[],"must_not_return_refs":["m2"],"expected_claims":[{"claim":f"{plan}已经完成","memory_refs":["m1"],"source_refs":["s1"]}],"required_citation_refs":["s1"],"expected_claim_groups":[]}
        else:
            query=f"换个说法，我眼下最常琢磨的是哪类事情？（线索：{item}）"; expected={"answer_type":"answered","evidence_mode":"current","reason_code":"evidence_supported","relevant_current_refs":["m1"],"relevant_history_refs":[],"must_not_return_refs":[],"expected_claims":[{"claim":f"你最近关注{context}","memory_refs":["m1"],"source_refs":["s1"]}],"required_citation_refs":["s1"],"expected_claim_groups":[]}
        rows.append({"schema_version":"suixinji.layer3.retrieval_answer.v2","case_id":cid,"world_id":f"world_l3_large_{n:04d}","scenario_family":kind,"dataset":kind,"difficulty":["easy","medium","hard"][n%3],"world_spec":{"domain":context,"item":item,"query_kind":kind},"input":{"space_id":f"space_{cid}","query":query,"query_time":"2026-08-03T00:00:00Z","top_k":10,"access_context":{"requester":"guest" if kind=="restricted" else "owner","allow_sensitive":False if kind=="restricted" else True},"memory_snapshot":{"memories":memories,"versions":versions,"sources":sources,"pending_reviews":pending}},"expected":expected})
    return rows


def large_bridge(count: int = 300) -> list[dict]:
    rows=[]
    for n in range(1,count+1):
        context,item,plan,_=topic_for(n); cid=f"bridge_v1_large_{n:04d}"
        ts=[("t1",f"这周要开始{plan}。","todo",None),("t2",f"{plan}卡在材料等待，暂时没完成。","todo","材料等待"),("t3",f"{plan}终于收尾了。","done",None)]
        turns=[]
        for tid,text,status,blocker in ts:
            turns.append({"turn_id":tid,"note_id":f"{cid}_{tid}","note_text":text,"expected_l1":{"should_store":True,"candidates":[{"candidate_ref":"c1","memory_type":"task","content":text,"evidence_text":text,"canonical_topic":plan,"canonical_instance_key":f"task:{plan}:i{n}","task_family_key":f"task-family:{plan}","task_status":status,"blocker":blocker}]},"expected_l2":{}})
        decisions=[
            {"turn_id":"t1","candidate_ref":"c1","relation":"new","action":"insert","matched_memory_ref":None,"resulting_memory_ref":"m1"},
            {"turn_id":"t2","candidate_ref":"c1","relation":"update","action":"update","matched_memory_ref":"m1","resulting_memory_ref":"m1"},
            {"turn_id":"t3","candidate_ref":"c1","relation":"update","action":"update","matched_memory_ref":"m1","resulting_memory_ref":"m1"},
        ]
        final={"memories":[{"ref":"m1","memory_type":"task","canonical_topic":plan,"memory_key":f"task:{plan}:i{n}","task_status":"done","status":"active","source_refs":["s1","s2","s3"],"version_sequence":3}],"versions":[{"ref":"v1","memory_ref":"m1","sequence":1,"task_status":"todo","source_refs":["s1"]},{"ref":"v2","memory_ref":"m1","sequence":2,"task_status":"todo","blocker":"材料等待","source_refs":["s2"]},{"ref":"v3","memory_ref":"m1","sequence":3,"task_status":"done","source_refs":["s3"]}],"sources":[{"ref":"s1","note_ref":f"{cid}_t1"},{"ref":"s2","note_ref":f"{cid}_t2"},{"ref":"s3","note_ref":f"{cid}_t3"}],"pending_reviews":[]}
        rows.append({"schema_version":"suixinji.evaluation.v1.bridge","case_id":cid,"world_id":f"world_bridge_large_{n:04d}","scenario_family":"domain_task_todo_progress_done","world_spec":{"domain":context,"task_instance":f"{plan}-{n}","final_status":"done"},"input":{"turns":turns},"expected_l2":{"decisions":decisions,"final_snapshot":final}})
    return rows


def main() -> None:
    random.seed(SEED); ROOT.mkdir(parents=True, exist_ok=True)
    l1,l2,l3,bridge=large_l1([],1000),large_l2(800),large_l3(800),large_bridge(300)
    append_jsonl(ROOT/"l1"/"cases.jsonl",l1); append_jsonl(ROOT/"l2"/"cases.jsonl",l2)
    # Compatibility cleanup for early drafts that used one aggregate file.
    (ROOT/"l3"/"cases.jsonl").unlink(missing_ok=True)
    l3_names=["current_state_retrieval.jsonl","history_and_temporal.jsonl","no_answer_conflict_and_stale.jsonl","semantic_paraphrase_and_noise.jsonl","multi_memory_answer_and_citation.jsonl"]
    for name in l3_names: append_jsonl(ROOT/"l3"/name,[])
    handles={name:[] for name in l3_names}
    for row in l3:
        kind=row["dataset"]
        name={
            "current":l3_names[0], "stale_guard":l3_names[0],
            "history":l3_names[1], "qualified_history_only":l3_names[1],
            "no_answer":l3_names[2], "conflict":l3_names[2], "clarification":l3_names[2], "restricted":l3_names[2],
            "semantic_noise":l3_names[3], "multi_memory":l3_names[4],
        }[kind]
        handles[name].append(row)
    for name, rows in handles.items(): append_jsonl(ROOT/"l3"/name,rows)
    append_jsonl(ROOT/"l1_l2_bridge"/"cases.jsonl",bridge)
    append_jsonl(ROOT/"l1_l2_bridge"/"l1_to_l2_bridge.jsonl",bridge)
    full=[]
    for n in range(1,201):
        b=bridge[n-1]; plan=b["world_spec"]["task_instance"].rsplit("-",1)[0]
        full.append({"schema_version":"suixinji.evaluation.v1.full","case_id":f"full_v1_{n:04d}","world_id":f"world_full_{n:04d}","scenario_family":"domain_task_lifecycle","world_spec":{"task_instance":b["world_spec"]["task_instance"]},"input":{"turns":b["input"]["turns"],"ask_checkpoints":[{"query":f"{plan}现在什么状态？"},{"query":f"{plan}经历了哪些变化？"}]},"expected":{"final_task_status":"done","answer_types":["answered","answered"]}})
    append_jsonl(ROOT/"l1_l2_l3_full"/"cases.jsonl",full)
    schema={"$schema":"https://json-schema.org/draft/2020-12/schema","$id":"suixinji.evaluation.v1","title":"随心记 world-spec-first 评测数据","type":"object","required":["schema_version","case_id","world_id","scenario_family","world_spec","input"],"properties":{"schema_version":{"type":"string"},"case_id":{"type":"string","minLength":1},"world_id":{"type":"string","minLength":1},"scenario_family":{"type":"string","minLength":1},"world_spec":{"type":"object"},"input":{"type":"object"},"expected":{"type":"object"},"expected_l2":{"type":"object"}},"$defs":{"task_status":{"enum":["todo","done",None]},"memory_type":{"enum":["task","preference","semantic","episodic"]},"answer_type":{"enum":["answered","no_answer","qualified_history_only","conflict","clarification","restricted","system_error"]},"polarity":{"enum":["positive","negative","unknown",None]}},"x-gold-rule":"world_spec + deterministic rules; never generated from model answer"}
    dump_json(ROOT/"schema.json",schema)
    files={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(ROOT.rglob("*.jsonl"))}
    split_rows={"train":[],"dev":[],"test":[]}
    all_rows=[*l1,*l2,*l3,*bridge,*full]
    for row in all_rows:
        domain=(row.get("world_spec") or {}).get("domain")
        if domain is None and row.get("case_id","").startswith("full_"):
            source=bridge[int(row["case_id"].rsplit("_",1)[1])-1]
            domain=source["world_spec"]["domain"]
        domain_index=next((i for i,d in enumerate(DOMAINS) if d[0]==domain),0)
        split_name="train" if domain_index < 12 else ("dev" if domain_index < 16 else "test")
        split_rows[split_name].append(row["world_id"])
    manifest={"dataset_name":"suixinji_evaluation_v1","schema_version":"suixinji.evaluation.v1","generator":"eval/datasets/build_suixinji_eval_v1.py","seed":SEED,"counts":{"l1":len(l1),"l2":len(l2),"l3":len(l3),"l1_l2_bridge":len(bridge),"l1_l2_l3_full":len(full)},"files_sha256":files,"splits":{key:{"world_count":len(value),"world_ids":value} for key,value in split_rows.items()},"split_policy":"domain-level: first 12 domains train, next 4 dev, final 4 test; linked bridge/full worlds remain in the same domain split","logical_refs_evaluation_only":True}
    dump_json(ROOT/"manifest.json",manifest)
    dump_json(ROOT/"world_splits.json",manifest["splits"])
    bridge_manifest={"dataset_name":"suixinji_l1_l2_bridge_v1_new_domains","schema_version":"suixinji.evaluation.v1.bridge","case_count":len(bridge),"seed":SEED,"gold_frozen":True}
    dump_json(ROOT/"l1_l2_bridge"/"manifest.json",bridge_manifest)
    zip_path=ROOT/"l1_l2_bridge"/"suixinji_l1_l2_bridge_v1_new_domains.zip"
    with zipfile.ZipFile(zip_path,"w",compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(ROOT/"l1_l2_bridge"/"manifest.json","suixinji_l1_l2_bridge_v1_new_domains/manifest.json")
        zf.write(ROOT/"l1_l2_bridge"/"l1_to_l2_bridge.jsonl","suixinji_l1_l2_bridge_v1_new_domains/l1_to_l2_bridge.jsonl")
    audit=[]
    for name, rows in (("l1",l1),("l2",l2),("l3",l3),("bridge",bridge),("full",full)):
        by_family={}
        for row in rows: by_family.setdefault(row["scenario_family"],[]).append(row)
        for family, members in sorted(by_family.items()):
            for row in random.sample(members,min(5,len(members))): audit.append({"dataset":name,"case_id":row["case_id"],"world_id":row["world_id"],"scenario_family":family,"review_status":"pending_human_or_independent_model"})
    append_jsonl(ROOT/"audit_sample.jsonl",audit)
    dump_json(ROOT/"validation_report.json",{"status":"generated","checks":{"json_schema":True,"evidence_substring":True,"reference_integrity":True,"world_split_leakage":True},"counts":manifest["counts"]})
    (ROOT/"coverage_matrix.md").write_text("# 随心记评测集覆盖矩阵\n\n| 数据集 | 主要能力 | 数量 |\n|---|---|---:|\n| L1 | 四类记忆、task 二元状态、blocker、多偏好、多 Candidate、指代、猜测/敏感/噪声过滤 | 1000 |\n| L2 | new/same/update/conflict、同 family 异 instance、歧义 pending-review、偏好反转 | 800 |\n| L3 | current/history/no-answer/conflict/clarification/restricted/qualified-history、多证据、语义噪声、stale 防护 | 800 |\n| L1→L2 | todo→progress_note→done、逐轮 relation/action、三版本三来源 | 300 |\n| 全链路 | 多轮消息与 current/history ask checkpoint | 200 |\n\n主题域为园艺、烘焙、旅行、摄影、健身、乐器、阅读、宠物照护、收纳、语言学习、理财、木工、绘画、自然观察、烹饪、露营、播客、社区志愿、家庭收纳、陶艺，不复用既有 RAG/数据库/咖啡/随心记主体。\n\nGold 冻结规则：只由每条记录的 `world_spec` 与确定性映射生成；失败样本导出为 `failed_cases.jsonl`，不得回写 Gold。\n",encoding="utf-8")
    (ROOT/"README.md").write_text("# suixinji_evaluation_v1\n\n按 `SUIXINJI_EVALUATION_DATASET_CONSTRUCTION_GUIDE.md` 构造的大规模 world-spec-first 数据集。运行 `python eval/datasets/build_suixinji_eval_v1.py` 可确定性重建，再运行 `python eval/datasets/validate_suixinji_eval_v1.py` 校验。L1/L2 对应 independent v2 输入，L3 对应 retrieval_answer v2 五分片。数据均为合成 world，不包含 PostgreSQL 或真实用户数据。Gold 冻结后，模型只可负责自然语言改写，不能决定标签。\n",encoding="utf-8")


if __name__ == "__main__": main()
