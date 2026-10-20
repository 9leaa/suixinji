from pathlib import Path
import json,html,collections
from independent_cases import OUT,CASES
E=lambda x:html.escape(str(x))
def pretty(x):return E(json.dumps(x,ensure_ascii=False,indent=2,default=str))
final_info=json.loads((OUT/'final-run.json').read_text()) if (OUT/'final-run.json').exists() else {}
status_text=('同版本完整复验已完成 · '+str(final_info.get('run_id'))) if final_info else '修复与重测中；以下统计为每题最近一次结果，尚非最终同版本完整复验'
alltraces=collections.defaultdict(list)
for p in sorted((OUT/'traces').glob('FM*.json')):
 try:r=json.loads(p.read_text());alltraces[r['id']].append(r)
 except Exception:continue
for p in sorted((OUT/'assessments').glob('FM*.json')) if (OUT/'assessments').exists() else []:
 try:
  r=json.loads(p.read_text());r['assessment_only']=True;alltraces[r['id']].append(r)
 except Exception:continue
for rows in alltraces.values():rows.sort(key=lambda x:x['started_at'])
latest={k:v[-1] for k,v in alltraces.items()}
real=[c for c in CASES if c['level'].startswith('真实')];struct=[c for c in CASES if not c['level'].startswith('真实')]
real_completed=sum(len(latest.get(c['id'],{}).get('evidence',{}).get('messages',[]))==len(c['messages']) and bool(latest.get(c['id'],{}).get('stages')) for c in real)
passed=lambda cs:sum(latest.get(c['id'],{}).get('status')=='pass' for c in cs)
counts=collections.Counter(latest.get(c['id'],{}).get('status','not_run') for c in CASES)
css='''*{box-sizing:border-box}body{margin:0;background:#f4f6fa;color:#1e2e43;font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}main{max-width:1180px;margin:auto;padding:36px 24px}h1{font-size:30px;margin:0 0 8px}h2{font-size:23px;margin:36px 0 8px}h3{font-size:18px;margin:8px 0}p{margin:8px 0}a{color:#245a9b}table{width:100%;border-collapse:collapse;background:white;margin:20px 0}td,th{padding:12px;text-align:left;border-bottom:1px solid #dce3ec}th{background:#223f65;color:white}.muted,small{color:#60728a}.banner{padding:17px 20px;background:#e7eef8;border-left:4px solid #456c9e;margin:20px 0}.metrics{display:flex;gap:14px;flex-wrap:wrap}.metric{flex:1;min-width:180px;background:white;padding:16px;border-radius:10px}.metric strong{display:block;font-size:28px}.filter{position:sticky;top:0;background:#f4f6faf2;padding:14px 0;z-index:2;display:flex;gap:10px;flex-wrap:wrap}input,select{padding:10px;border:1px solid #bccbdc;border-radius:6px;font-size:14px}input{flex:1;min-width:200px}article{background:white;border:1px solid #dce3ec;border-radius:10px;padding:19px 22px;margin:14px 0}.tags{font-size:13px;color:#466785;font-weight:600}.evidence{background:#f0f4fa;padding:8px 12px}details{border-top:1px solid #edf0f5;padding-top:7px;margin-top:9px}summary{cursor:pointer;color:#456582}pre{font:13px/1.7 -apple-system,"PingFang SC",sans-serif;white-space:pre-wrap;overflow-wrap:anywhere;background:#f7f9fc;padding:13px;max-height:650px;overflow:auto}.pass{border-left:5px solid #4a866d}.fail{border-left:5px solid #b65e5e}.blocked,.not_run{border-left:5px solid #b28b4a}footer{margin-top:30px;font-size:13px;color:#61748b}@media(max-width:650px){main{padding:20px 12px}td,th{padding:8px;font-size:12px}h1{font-size:25px}}'''
parts=[f'<!doctype html><html lang="zh-CN"><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>语义/情景记忆 · 全部问题分类汇总</title><style>{css}</style><main><h1>语义 / 情景记忆 · 第 1 轮独立测试</h1><p class="muted">固定 32 组 · 计划 97 条消息 / 操作叙述 · 18 组真实模型消息链路 · 14 组结构性验证</p><p class="muted">{E(status_text)}</p><div class="metrics">']
for label,note,value in [('真实链路通过',f'已完成评分{real_completed}组 / 计划18组；不混入结构测试',f'{passed(real)} / {real_completed}'),('结构验证通过','直接事实注入 + 固定计划回调',f'{passed(struct)} / {len(struct)}'),('失败 / 阻塞 / 未跑','包括运行错误，不能等同语义错误',f'{counts["fail"]} / {counts["blocked"]} / {counts["not_run"]}'),('人工复核','自动判定与助手证据抽查','0 组')]:parts.append(f'<div class="metric">{label}<strong>{value}</strong>{note}</div>')
parts.append('</div><div class="banner">专业：真实消息链路 = 保存 Note → process_note_memory 抽取 → 多轮整理 → memory_search 召回 → answer_question 回答。结构测试只验证状态、审批和并发约束。<br>直白：真实模型测试看它是否理解并记对；结构测试看程序是否守住规则。自动评分不代表人工复核。首次结果和重测结果均保留，预期不随结果修改。评分修订单独记录：改用问答实际取证，不要求旧task/preference进入新卡，也不因合法原始Note来源扣分；不会把评分调用失败算业务失败。初始协议故障 run 150054 的18组在第一条消息后中断；run 150311 因review协议错误停止。二者不能作为完整多轮语义正确率。</div><h2>真实链路分阶段正确率</h2><table><tr><th>阶段</th><th>通过 / 已判定</th><th>失败 / 未跑</th><th>说明</th></tr>')
for key,label,desc in [('extraction','抽取','原消息是否正确变成事实；不拿派生卡补证'),('consolidation','整理','对象/事件/历史与推断是否整理正确'),('retrieval','召回','问答实际工具/上下文能否覆盖所问事实'),('answer','回答','现有问答代理实际回答是否正确')]:
 rows=[latest.get(c['id'],{}).get('stages',{}).get(key,{}) for c in real];n=sum(isinstance(x.get('passed'),bool) for x in rows);p=sum(x.get('passed') is True for x in rows);failed=sum(x.get('passed') is False for x in rows);not_run=18-n
 parts.append(f'<tr><td>{label}</td><td>{p} / {n}'+(f'（{p/n:.1%}）' if n else '（未完成评分）')+f'</td><td>{failed} / {not_run}</td><td>{desc}</td></tr>')
groups=json.loads((OUT/'issue-groups.json').read_text()) if (OUT/'issue-groups.json').exists() else []
acl_before=json.loads((OUT/'auxiliary-acl-audit-before.json').read_text()) if (OUT/'auxiliary-acl-audit-before.json').exists() else {}
acl_after=json.loads((OUT/'auxiliary-acl-audit-after.json').read_text()) if (OUT/'auxiliary-acl-audit-after.json').exists() else {}
parts.append('</table><h2>补充权限审计 <small>单独统计，不计入原32组</small></h2>')
parts.append(f'<div class="banner">公共事实与Alice私有事实组合成卡：修复前Bob能读到私有访谈；修复后卡继承全部来源的严格权限，Bob命中{len(acl_after.get("bob_hits",[]))}条，Alice命中{len(acl_after.get("alice_hits",[]))}条。修复验证：{"通过" if acl_after.get("passed") else "未通过 / 未跑"}。</div><details><summary>权限审计前后原始证据</summary><pre>{pretty({"before":acl_before,"after":acl_after})}</pre></details>')
stability=json.loads((OUT/'stability-fm12-summary.json').read_text()) if (OUT/'stability-fm12-summary.json').exists() else {}
if stability:
 parts.append(f'<h2>FM12 后续稳定性附测 <small>不并入完整轮17/18</small></h2><div class="banner">新增疑问整理规则后，{stability.get("valid_total",0)}次有效全新空间附测，{stability.get("valid_pass",0)}次通过；另{stability.get("invalid_framework_attempts",0)}次为答错问题ID的框架无效尝试，完整保留但不计分。</div>')
 for t in stability.get('trials',[]):
  title=f'尝试{t["trial"]} · {t["status"]} · {t.get("space","")}'
  raw=json.loads(Path(t['trace']).read_text()) if t.get('trace') and Path(t['trace']).exists() else {}
  parts.append(f'<details><summary>{E(title)}</summary><pre>{pretty({"summary":t,"questions":raw.get("final_state",{}).get("question"),"cards":raw.get("final_state",{}).get("card"),"answer":raw.get("evidence",{}).get("answer"),"stages":raw.get("stages")})}</pre></details>')
if final_info:parts.append(f'<details><summary>冻结运行版本、源码hash、后续补丁与附测边界</summary><pre>{pretty(final_info)}</pre></details>')
parts.append('<h2>共性问题分类</h2><table><tr><th>主分类</th><th>专业解释 / 直白解释</th><th>出现过问题题号</th></tr>')
for g in groups:
 parts.append(f'<tr><td>{E(g["category"])}</td><td>{E(g["professional"])}<br>{E(g["plain"])}</td><td>'+ '、'.join(f'<a href="#{x}">{x}</a>' for x in g['cases'])+'</td></tr>')
parts.append('</table><h2>分类统计</h2><table><tr><th>分类</th><th>通过 / 总数</th><th>问题序号</th></tr>')
for cat in dict.fromkeys(c['category'] for c in CASES):
 cs=[c for c in CASES if c['category']==cat];ids='、'.join(c['id'] for c in cs if latest.get(c['id'],{}).get('status')!='pass');parts.append(f'<tr><td>{cat}</td><td>{passed(cs)} / {len(cs)}</td><td>{ids or "无"}</td></tr>')
parts.append('</table><div class="filter"><input id="search" placeholder="搜索编号、主题、原文、错误、建议"><select id="category"><option value="">全部分类</option>')
for cat in dict.fromkeys(c['category'] for c in CASES):parts.append(f'<option>{cat}</option>')
parts.append('</select><select id="status"><option value="">全部结果</option><option value="pass">通过</option><option value="fail">失败</option><option value="blocked">阻塞</option><option value="not_run">未跑</option></select><select id="level"><option value="">全部验证层级</option><option value="real">真实模型</option><option value="struct">结构性验证</option></select><span id="count">32 组</span></div>')
checks=json.loads((OUT/'checkpoints.json').read_text()) if (OUT/'checkpoints.json').exists() else {}
labels={'pass':'通过','fail':'失败','blocked':'阻塞','not_run':'未跑'}
for c in CASES:
 r=latest.get(c['id'],{});status=r.get('status','not_run');history=alltraces[c['id']];parts.append(f'<article id="{c["id"]}" class="{status}" data-category="{c["category"]}" data-status="{status}" data-level="{"real" if c["level"].startswith("真实") else "struct"}"><div class="tags">{c["id"]} · {c["category"]} · {labels[status]} · {len(history)} 次记录</div><h3>{c["title"]}</h3><p><b>验证层级：</b>{c["level"]}</p><p class="evidence"><b>最终判定（自动 + 助手证据抽查）：</b>{E(r.get("reason","尚未运行"))}</p>')
 issues=r.get('issues',[]);suggestion='；'.join(x.get('suggestion','') for x in issues) or ('检查真实轨迹中失败阶段及其上游；修复后仅重测失败项，保留首次结果。' if status!='pass' else '当前检查点通过，后续轮次使用全新主体和主题。')
 parts.append(f'<p><b>修复建议：</b>{E(suggestion)}</p><details><summary>原始消息 / 操作叙述与固定期望</summary><pre>{pretty({"messages":c["messages"],"query":c["query"],"expected":c["expected"],"checkpoints":checks.get(c["id"],{})})}</pre></details><details><summary>各阶段结果与失败证据</summary><pre>{pretty({"stages":r.get("stages",{}),"issues":issues,"exception":r.get("exception")})}</pre></details>')
 e=r.get('evidence',{});parts.append(f'<details><summary>前后结果：首条整理 → 最终整理 → 查询 → 回答</summary><pre>{pretty({k:e.get(k) for k in ["after_first","before_final","after_final","questions_before_answer","answer_snapshot","query_hits","answer","answer_error"]})}</pre></details><details><summary>首次 / 重测差异与每次完整原始 trace</summary>')
 for j,h in enumerate(history):parts.append(f'<details><summary>第 {j+1} 次 · {E(h.get("started_at"))} · {labels[h["status"]]} · {"评分修订，未重跑业务 · " if h.get("assessment_only") else ""}{E(h.get("reason"))}</summary><pre>{pretty(h)}</pre></details>')
 parts.append('</details></article>')
parts.append('''<footer>仅使用独立数据库 suixinji_fact_memory_test_20261020。cases.json / checkpoints.json 为固定标准；results.json 为运行结果；traces/ 保留逐题原始记录。未通过的事实归类不等于独立缺陷数；同一个故障可影响多题。</footer></main><script>const ids=['search','category','status','level'];function filter(){let n=0;for(const a of document.querySelectorAll('article')){let ok=a.textContent.toLowerCase().includes(document.getElementById('search').value.toLowerCase());for(const k of ids.slice(1)){const v=document.getElementById(k).value;ok=ok&&(!v||a.dataset[k]===v)}a.hidden=!ok;if(ok)n++}document.getElementById('count').textContent=n+' 组'}for(const id of ids)document.getElementById(id).addEventListener('input',filter)</script></html>''')
p=OUT/'全部问题分类汇总.html';p.write_text(''.join(parts));print(p)
