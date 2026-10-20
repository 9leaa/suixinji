"""Render frozen independent runs; never infer a score from a partial rerun."""
from pathlib import Path
import collections
import html
import json
import sys

OUT = Path(sys.argv[1])
manifest = json.loads((OUT / "three-rounds.json").read_text())
reference = Path("/Users/zhangchengjie/Projects/suixinji/workspace/outputs/issues-all-20261019/全部问题分类汇总.html").read_text()
css = reference.split("<style>", 1)[1].split("</style>", 1)[0]
css += ".pass{border-left:4px solid #4a866d}.fail{border-left:4px solid #b65e5e}img{max-width:100%;max-height:650px}pre{max-height:550px;overflow:auto}[hidden]{display:none!important}"
esc = lambda value: html.escape(str(value))
pretty = lambda value: esc(json.dumps(value, ensure_ascii=False, indent=2, default=str))
read = lambda name: json.loads((OUT / name).read_text())
details = lambda title, value: f"<details><summary>{esc(title)}</summary><pre>{pretty(value)}</pre></details>"
parts = [f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>语义与情景记忆 · 三轮测试分类汇总</title><style>{css}</style><main><h1>语义与情景记忆 · 三轮测试分类汇总</h1><p class="muted">独立数据 · 冻结预期 · 初测与复测分开 · 原文和实际回答可展开</p>']
parts.append('<div class="banner">每轮由新建的 GPT-6.1-sol 高推理子代理独立编写数据，产品调用 DeepSeek v4 flash。准确率只统计真实消息链路；结构断言、浏览器审批测试另列。自动模型评分、第二模型复核与助手证据检查均不冒充人工标注。失败项复测不与完整轮拼成新的分数。</div>')
parts.append('<table><tr><th>轮次 / 消息</th><th>初测</th><th>最终完整轮</th><th>失败项复测 / 附测</th></tr>')
datasets = []
for config in manifest["rounds"]:
    cases = read(config["cases"])["cases"]
    before = read(config["baseline"])
    after = read(config["final"])
    def real(rows):
        return [r for r in rows if r.get("level", "真实").startswith("真实")]
    initial, final = real(before["results"]), real(after["results"])
    count = lambda rows: f'{sum(r["status"] == "pass" for r in rows)}/{len(rows)}'
    pct = sum(r["status"] == "pass" for r in final) / len(final)
    parts.append(f'<tr><td>第{config["round"]}轮<br>{len(cases)}组 / {sum(len(c["messages"]) for c in cases)}条消息或操作叙述</td><td>{count(initial)}<br>{esc(config.get("baseline_note", ""))}</td><td><b>{count(final)} · {pct:.2%}</b><br>运行 {esc(after["metadata"]["run_id"])}</td><td>{esc(config["retest_note"])}</td></tr>')
    datasets.append((config, cases, before, after))
parts.append('</table><h2>召回检查：按实际返回证据计分</h2><p>下表是案例级检查点通过率，不是穷举所有相关文档后的 Recall@K。第一轮只保留合并召回评分；第二、三轮分别检查即时、整理和查询记录，评分只能引用对应阶段实际返回的 ID 与原文片段。</p><table><tr><th>轮次</th><th>即时召回</th><th>整理后召回</th><th>查询实际召回</th><th>最终回答</th></tr>')
for config, cases, before, after in datasets:
    rows = [r for r in after["results"] if r.get("level", "真实").startswith("真实")]
    values = []
    for stage in ("immediate_retrieval", "consolidated_retrieval", "query_retrieval", "answer"):
        judgments = [r["stages"][stage] for r in rows if stage in r.get("stages", {})]
        unrun = sum(j.get("passed") is None for j in judgments)
        values.append((f'{sum(j["passed"] is True for j in judgments)}/{len(judgments)}'
                       + (f"（{unrun}组未运行）" if unrun else "")) if judgments else "未单独计分")
    parts.append(f'<tr><td>第{config["round"]}轮</td>' + "".join(f"<td>{esc(v)}</td>" for v in values) + "</tr>")
parts.append('</table><h2>实际方案：即时处理与整理</h2><table><tr><th>环节</th><th>技术说明</th><th>直白解释</th></tr>')
for row in [
    ("接收与分类", "保存 Note；任务、偏好保留原抽取协议；事实采用独立自然语言抽取", "先留原话，再按类型处理"),
    ("即时证据", "fact：原文跨度、来源版本、类型、时间、指代状态、上下文依据", "没整理完也能查询；不知道是谁就保留疑问"),
    ("整理触发", "上海时间每日03:00；每类未处理事实≥30；手动触发", "每天一次，积累多了提前做，测试时可立即做"),
    ("整理工具", "search / inspect / create / add_evidence / revise / merge / split / link / resolve_reference / defer / retract", "先找证据，再新建、补充、合并、拆开、关联或询问"),
    ("写入校验", "回滚事务预检 + 独立语义审阅 + 版本校验 + 同空间租约", "参数和证据都查过才提交，不覆盖用户刚改的内容"),
    ("数据形式", "粗粒度自然语言对象/事件卡；stated与inferred分开", "不要求每个领域设计固定属性表"),
    ("归纳审阅", "显示推断标签、生成时间、来源、版本；支持确认/编辑/否定", "系统推断不会冒充用户亲口说过的事实"),
    ("删除", "候选预览→逐项/全选审批→遗忘标记；屏蔽问答证据中的已忘跨度", "删记忆、留笔记，并阻止回答又从原文读回"),
    ("来源修改", "抽取前同步来源版本，连空抽取也失效旧事实及依赖卡", "原话改了，旧结论不能继续当真"),
]:
    parts.append("<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>")
parts.append('</table><h2>三个阶段如何召回</h2><table><tr><th>阶段</th><th>实际机制</th><th>检查依据</th></tr>')
for row in [
    ("即时抽取", "同空间词面+语义向量前12条，补最近8条；排除本条，保留所用上下文ID", "fm_extraction_context与逐消息snapshot"),
    ("定期整理", "每条新事实召回前8条，显式补证关联必取；卡片再展开来源；最多3次search/inspect", "fm_run中实际读取结果、模型方案与审阅"),
    ("查询回答", "普通搜索、历史查询、事件列表统一接入事实/卡；ACL先过滤；原文回退尊重遗忘", "问答实际工具trace及最终回答，外部limit50只用于诊断"),
]:
    parts.append("<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in row) + "</tr>")
parts.append('</table><p>语义召回使用现有 embedding provider；词面分数与语义相似度融合。向量按模型、端点、维度和内容缓存，召回记录保留两个分数；提供方失败时标记退化为词面检索。相似仅表示值得检查，不等于同一主体或删除授权。</p>')
parts.append('<h2>共性问题、原因与修复</h2><table><tr><th>分类</th><th>原因</th><th>通用修改 / 验证</th></tr>')
for fix in manifest["fixes"]:
    parts.append("<tr>" + "".join(f"<td>{esc(fix[k])}</td>" for k in ("category", "cause", "fix")) + "</tr>")
parts.append('</table><h2>用户检查与审批</h2><p>统一入口：<a href="http://127.0.0.1:18080/console">控制台 → 记忆整理与审批</a>。支持手动整理、回答疑问、原文证据、归纳时间、确认/编辑/否定、修改历史、主题删除预览和单条/批量审批。</p>')
parts.append('<p>浏览器真实操作与数据库复核：归纳确认后仍标为归纳；编辑保留版本；否定保留来源；删除后原始笔记不变。UI夹具不计入模型正确率。</p>')
for name in ["控制台记忆入口.png", "归纳修改界面.png", "批量审批界面.png", "部分删除后重新整理.png"]:
    if (OUT / name).exists():
        parts.append(f'<details><summary>{esc(name[:-4])}</summary><img src="{esc(name)}" alt="{esc(name[:-4])}"></details>')
parts.append(details("独立数据库核对", read("ui-verification.json")))
parts.append(details("部分删除后再次整理：原笔记保留，已忘内容未重建", read("ui-partial-forget-after.json")))
parts.append('<h2>逐条证据与分类</h2><div class="filter"><input id="search" placeholder="搜索题号、主题、原文或原因"><select id="round" aria-label="轮次"><option value="">全部轮次</option><option value="1">第1轮</option><option value="2">第2轮</option><option value="3">第3轮</option></select><select id="category" aria-label="问题分类"><option value="">全部分类</option></select><select id="status" aria-label="结果"><option value="">全部结果</option><option value="fail">完整轮未通过</option><option value="pass">完整轮通过</option><option value="repaired">曾失败后通过</option></select><span id="count"></span></div>')
for config, cases, before, after in datasets:
    initial = {r["id"]: r for r in before["results"]}
    final = {r["id"]: r for r in after["results"]}
    parts.append(f'<section><h2>第{config["round"]}轮</h2>')
    parts.append(details("冻结来源及运行元数据", {"config": config, "initial": before["metadata"], "final": after["metadata"]}))
    for case in cases:
        start, end = initial.get(case["id"], {}), final.get(case["id"], {})
        evidence = end.get("evidence", {})
        status = end.get("status", "not_run")
        repaired = start.get("status") != "pass" and status == "pass"
        parts.append(f'<article class="{esc(status)}" data-round="{config["round"]}" data-category="{esc(case["category"])}" data-status="{esc(status)}" data-repaired="{str(repaired).lower()}"><div class="tags">{esc(case["id"])} · {esc(case["category"])} · {esc(case.get("level", "真实模型链路"))}</div><h3>{esc(case["title"])}</h3><p>初测：{esc(start.get("status", "未跑"))} → 完整复验：<b>{esc(status)}</b></p><p>{esc(end.get("reason", ""))}</p>')
        parts.append(details("原始消息与固定预期", {"messages": case["messages"], "query": case["query"], "expected": case["expected"]}))
        parts.append(details("初测失败原因与判定", {k: start.get(k) for k in ("reason", "issues", "stages")}))
        parts.append(details("实际回答与各阶段判定", {"answer": evidence.get("answer"), "stages": end.get("stages"), "issues": end.get("issues")}))
        parts.append(details("最终记忆、问题与实际召回", {"cards": end.get("final_state", {}).get("card"), "questions": end.get("final_state", {}).get("question"), "query_hits": evidence.get("query_hits"), "actual_answer_records": evidence.get("actual_answer_records")}))
        parts.append(details("整理模型方案与校验反馈", [
            {k: run.get(k) for k in ("id", "status", "error", "plan", "trace")}
            for run in end.get("final_state", {}).get("run", [])]))
        parts.append("</article>")
    parts.append("</section>")
parts.append(details("验证边界与已知限制", manifest["limitations"]))
parts.append('<footer>原始评分、复核与完整trace保存在同目录各轮子目录。各轮为有限合成集，不等于开放世界准确率；未将结构断言或UI操作混入模型正确率。</footer></main><script>const $=id=>document.getElementById(id);for(const category of [...new Set([...document.querySelectorAll("article")].map(a=>a.dataset.category))].sort()){const option=document.createElement("option");option.value=category;option.textContent=category;$("category").append(option)}function filter(){let count=0;for(const item of document.querySelectorAll("article")){const status=$("status").value;item.hidden=!(item.textContent.toLowerCase().includes($("search").value.toLowerCase())&&(!$("round").value||item.dataset.round===$("round").value)&&(!$("category").value||item.dataset.category===$("category").value)&&(!status||(status==="repaired"?item.dataset.repaired==="true":item.dataset.status===status)));if(!item.hidden)count++}$("count").textContent=count+"条"}for(const id of ["search","round","category","status"])$(id).addEventListener("input",filter);filter();</script></html>')
(OUT / "三轮测试分类汇总.html").write_text("".join(parts))
print(OUT / "三轮测试分类汇总.html")
