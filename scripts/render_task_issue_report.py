"""Render the task-memory issue audit using the user's reference report style."""
import argparse
from html import escape
import json
from pathlib import Path
import re

BASE = Path(__file__).resolve().parents[1]
RUN = BASE / 'eval/results/task-cards-100-live-20261020'


def pretty(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def fold(title, content):
    return f'<details><summary>{escape(title)}</summary><pre>{escape(content)}</pre></details>'


def card_text(cards):
    if not cards:
        return '没有已保存的任务卡片。'
    lines = []
    labels = {'requirement': '要求', 'progress': '进展', 'blocker': '阻碍', 'time': '时间', 'outcome': '结果', 'note': '备注'}
    for card in cards:
        lines.append(f"{card['subject']} · {card['task']}\n卡片 ID：{card['id']}\n轮次：{card['instance'] or '未指定'}\n状态：{card['task_status']}；结束原因：{card['closure_reason'] or '无'}")
        for detail in card['details']:
            condition = f"（条件：{detail['condition']}）" if detail['condition'] else ''
            lines.append(f"  · {labels[detail['kind']]}{condition}：{detail['description']}")
        lines.append('')
    return '\n'.join(lines).rstrip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    css = re.search(r'<style>(.*?)</style>', args.reference.read_text(), re.S).group(1)
    taxonomy = json.loads((BASE / 'eval/reports/task-cards-100-issue-taxonomy-20261020.json').read_text())
    cases = {c['id']: c for c in json.loads((RUN / 'dataset.json').read_text())['cases']}
    numbers = {}
    for cid, case in cases.items():
        for step in range(1, len(case['steps']) + 1):
            numbers[cid, step] = len(numbers) + 1
    result = json.loads((RUN / 'rescored-summary.json').read_text())
    scores = {(c['id'], row['step']): row['errors'] for c in result['results'] for row in c['failed_steps']}
    review = json.loads((RUN / 'manual-audit.json').read_text())['findings']
    audit = {(a['case'], a['step']): a['reason'] for a in review}
    groups = list(taxonomy['groups']) + [
        {'id': 'E23', 'name': '评分规则误报（仍计入自动失败）', 'plain': '保存内容语义可接受，但检查规则扣了分；本页不改分。', 'priority': '评分复核',
         'cause': '条件类型断言过严；对全部细节做字面禁词检查，没有区分有效要求与撤销说明。',
         'fix': '检查事实和条件是否完整，区分当前有效要求与否定说明。', 'accept': '允许合理的条件表达；“A4已作废”不再被判为A4要求仍有效。',
         'evidence': [{'case': 'details_01', 'step': 1}, {'case': 'details_03', 'step': 2}]},
        {'id': 'E1', 'name': '已修正的评分误报', 'plain': '数据库已有待确认，旧评分器却没有读到；现已纠正。', 'priority': '已修复',
         'cause': '服务 results 为空，但相同 note_id 已在数据库生成待确认。旧评分器只检查返回结果。',
         'fix': '已按原消息 ID 核对数据库待确认，并保存当步 pending_items；仅重评原输出，没有重跑模型。',
         'accept': '歧义消息不建错卡，并正确识别其已生成的待确认。',
         'evidence': [{'case': 'references_04', 'step': 2}, {'case': 'references_07', 'step': 1}]},
    ]
    sections, table_rows = [], []
    total = 0
    for group in groups:
        entries = []
        for ev in group['evidence']:
            cid, step = ev['case'], ev['step']
            rows = [json.loads(line) for line in (RUN / cid / 'steps.jsonl').read_text().splitlines()]
            row = rows[step - 1]
            scope = ('已修正的评分误报' if group['id'] == 'E1' else
                     '自动通过的附加问题' if group['id'] == 'T7' else '当前自动未通过')
            status = ('评分已修正 · 自动通过' if group['id'] == 'E1' else
                      '自动通过 · 人工发现重复' if group['id'] == 'T7' else
                      '自动未通过 · 规则误报' if group['id'] == 'E23' else '自动未通过 · 实现或契约问题')
            errors = scores.get((cid, step), [])
            handling = [{k: value for k, value in action.items() if k != 'task_card'} for action in row['report'].get('results', [])]
            history = '\n'.join(f"第 {r['step']} 步 [{r['message_time']}] {r['text']}" for r in rows[:step])
            source = f"场景：{cid}\n步骤：{step}\nTrace ID：{row['report'].get('trace_id', '无')}\n{RUN / cid / 'steps.jsonl'}（第 {step} 行）"
            clue = audit.get((cid, step)) or ('；'.join(errors) if errors else group['plain'])
            note = f"<p class='evidence'><b>连锁影响：</b>{escape(ev['note'])}</p>" if ev.get('note') else ''
            details = ''.join([
                fold('原消息', row['text']),
                fold('本步预期与场景说明', pretty(row['expected']) + '\n\n场景整体判定说明（可能包含后续步骤）：\n' + cases[cid]['oracle']),
                fold('实际保存结果', card_text(row['cards'])),
                fold('本步之前的卡片', card_text(rows[step - 2]['cards']) if step > 1 else '场景开始前没有任务卡片。'),
                fold('处理结果与自动检查', pretty({'处理状态': row['report'].get('extraction_status'), '操作': handling, '修正评分器后的错误': errors})),
                fold('模型原始输出', pretty([{'阶段': call['stage'], '输出': call['output']} for call in row['model_calls']])),
                fold('已有排查记录与验收标准', group['cause'] + '\n\n验收标准：' + group['accept']),
                fold('可见会话原文（截至本步）', history),
                fold('来源位置', source),
            ])
            entries.append(f'''<article id="case-{cid}-{step}" data-category="{escape(group['name'], quote=True)}" data-scope="{scope}">
<div class="tags">#{numbers[cid, step]} · {escape(cid)} · 第 {step} 步 · {status} · {group['priority']}</div>
<h3>{escape(cases[cid]['name'])}</h3><p>{escape(clue)}</p>
<p class="evidence"><b>证据程度：</b>已核对原始模型输出、处理记录与保存快照；评分/实现问题分别标注。</p>{note}
<p><b>建议：</b>{escape(group['fix'])}</p><small>同一条消息只计一个主分类；完整证据可在下方展开。</small>{details}</article>''')
        n = len(entries)
        total += n
        ids = '、'.join(str(numbers[e['case'], e['step']]) for e in group['evidence'])
        table_rows.append(f'<tr><td><a data-category-target="{escape(group["name"], quote=True)}" href="#{group["id"]}">{escape(group["name"])}</a></td><td>{n}</td><td>{escape(group["plain"])}</td><td>{escape(ids)}</td></tr>')
        sections.append(f'<section class="issue-section" id="{group["id"]}"><h2>{escape(group["name"])} <span class="group-count">{n} 条</span></h2><p class="muted">专业：{escape(group["name"])}<br>直白：{escape(group["plain"])}</p>{"".join(entries)}</section>')
    assert total == 30
    options = ''.join(f'<option>{escape(g["name"])}</option>' for g in groups)
    extra_css = '''
[hidden]{display:none!important}html{scroll-behavior:smooth}.table-wrap{overflow-x:auto}td{overflow-wrap:anywhere}.table-wrap td:first-child{min-width:160px}.table-wrap td:nth-child(2){white-space:nowrap}button{padding:9px 13px;border:1px solid #bccbdc;border-radius:6px;background:#fff;color:#284f7e;font:inherit;cursor:pointer}button:hover{background:#e7eef8}a:focus-visible,summary:focus-visible,input:focus-visible,select:focus-visible,button:focus-visible{outline:3px solid #7da8de;outline-offset:3px}#count{align-self:center;white-space:nowrap}article{scroll-margin-top:90px}.tags{overflow-wrap:anywhere}#empty{padding:30px;text-align:center;background:white;border:1px solid #dce3ec;border-radius:10px}section{scroll-margin-top:100px}footer details pre{font-size:13px}@media(max-width:650px){.table-wrap table{min-width:710px}.filter{gap:8px}.filter input{flex-basis:100%}.filter select{min-width:0;max-width:100%;flex:1}.metric{min-width:135px}.tags{font-size:12px}article{padding:16px}section{scroll-margin-top:185px}}@media print{.filter{position:static}details{break-inside:avoid}article{break-inside:avoid}.table-wrap{overflow:visible}body{background:white}}
'''
    script = '''
const cards=[...document.querySelectorAll('article')],query=document.getElementById('search'),cat=document.getElementById('category'),scope=document.getElementById('scope');
function filter(){let n=0;const term=query.value.trim().toLowerCase();for(const c of cards){const show=(!term||c.textContent.toLowerCase().includes(term))&&(!cat.value||c.dataset.category===cat.value)&&(!scope.value||c.dataset.scope===scope.value);c.hidden=!show;if(show)n++}for(const s of document.querySelectorAll('.issue-section')){const all=[...s.querySelectorAll('article')],visible=all.filter(c=>!c.hidden).length;s.hidden=!visible;s.querySelector('.group-count').textContent=visible===all.length?all.length+' 条':visible+' / '+all.length+' 条'}document.getElementById('count').textContent=n+' / '+cards.length+' 条';document.getElementById('empty').hidden=n!==0}
[query,cat,scope].forEach(e=>e.addEventListener('input',filter));
document.getElementById('reset').addEventListener('click',()=>{query.value='';cat.value='';scope.value='';filter()});
document.querySelectorAll('[data-category-target]').forEach(a=>a.addEventListener('click',()=>{query.value='';scope.value='';cat.value=a.dataset.categoryTarget;filter()}));filter();
'''
    source_info = '\n'.join([str(BASE / 'docs/task-memory-issues-report-20261020.md'), str(BASE / 'eval/reports/task-cards-100-issue-taxonomy-20261020.json'), '被测业务版本：bb95406；评分器及测试结果：9accd89；问题分类：7085510。'])
    html = f'''<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="icon" href="data:,"><title>任务记忆 · 100 场景问题分类汇总</title><style>{css}{extra_css}</style></head><body><main>
<h1>任务记忆 · 100 场景问题分类汇总</h1><p class="muted">2026-10-20 · 100 个场景 / 222 轮消息 · 真实模型 deepseek-v4-flash · 沿用既有评分与人工复核口径</p>
<div class="metrics"><div class="metric">自动检查通过<strong>197 / 222</strong>88.7%；77 / 100 场景全部通过</div><div class="metric">当前自动未通过<strong>25 条</strong>23 条实现/契约问题 + 2 条规则误报</div><div class="metric">通过项的附加问题<strong>3 条</strong>同义要求、阻碍重复；未计入自动失败</div></div>
<div class="banner">本页列出 <b>30 条复核记录</b>：25 条自动未通过 + 3 条通过项附加问题 + 2 条已修正的评分误报。已观察到实现/质量问题为 26 条、涉及 24 个场景，包含连锁影响，不等于 26 个独立故障。自动通过率也不等于完整语义准确率。状态仍只有 <b>todo / done</b>。</div>
<div class="table-wrap"><table><thead><tr><th>主分类</th><th>条数</th><th>直白解释</th><th>消息序号</th></tr></thead><tbody>{''.join(table_rows)}</tbody></table></div>
<p><b>建议先处理：</b>冻结评分口径 → 统一抽取结构与任务身份 → 修复阻碍解除、多任务更新、明确纠正和转交 → 细节去重，再跑完整回归。</p>
<div class="filter"><input id="search" aria-label="搜索问题" placeholder="搜索场景编号、人物、原因或原文"><select id="category" aria-label="问题分类"><option value="">全部分类</option>{options}</select><select id="scope" aria-label="评分范围"><option value="">全部范围</option><option>当前自动未通过</option><option>自动通过的附加问题</option><option>已修正的评分误报</option></select><button id="reset" type="button">重置</button><span id="count" role="status" aria-live="polite">30 / 30 条</span></div>
<p id="empty" hidden>没有匹配记录。可调整关键词、分类或评分范围。</p>{''.join(sections)}
<footer><b>统计与验证边界：</b># 为数据集中222条消息的顺序号，另附场景与步骤用于定位。每条记录只计一个主分类，交叉问题保留在说明中。原分195/222因补读两条数据库待确认修正为197/222，没有重跑模型挑选结果。剩余两处规则误报仍保留原分；通过项仅做抽查。271份快照的状态约束和来源可追溯检查通过，无处理/查询运行时异常。未验证最终自然语言回答、飞书送达、查询排名和完整召回。业务代码、原始测试输出及线上数据未修改。
{fold('尚未证实发生的测试覆盖缺口', '状态和结束原因多数按整体集合比较，可能漏掉任务间归属互换；查询只检查非空。这是代码可见的测试盲区，不作为已发生故障计数。')}
{fold('报告来源与版本', source_info)}</footer></main><script>{script}</script></body></html>'''
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html)
    print(f'Created {args.output}: {total} records, {len(groups)} categories, {len(html.encode())} bytes')


if __name__ == '__main__':
    main()
