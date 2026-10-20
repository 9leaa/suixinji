"""Replay synthetic conversations through real extraction/association in isolated SQLite databases."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import sqlite3
import time


def check(expected, cards, before, reports, *, pending_for_note=False):
    errors = []
    aliases = expected.get('subject_aliases', {})
    def subject_of(card):
        return aliases.get(card['subject'], card['subject'])
    if len(cards) != expected['cards']:
        errors.append(f"卡片数：期望 {expected['cards']}，实际 {len(cards)}")
    if 'statuses' in expected and sorted(c['task_status'] for c in cards) != sorted(expected['statuses']):
        errors.append('状态不符：' + str([c['task_status'] for c in cards]))
    if 'closures' in expected and sorted(str(c['closure_reason']) for c in cards) != sorted(str(v) for v in expected['closures']):
        errors.append('结束原因不符：' + str([c['closure_reason'] for c in cards]))
    if 'same_as' in expected and {c['id'] for c in cards} != set(before[expected['same_as']]):
        errors.append('应更新原卡片，但卡片 ID 改变')
    if 'subjects' in expected and sorted(subject_of(c) for c in cards) != sorted(expected['subjects']):
        errors.append('执行人不符：' + str([c['subject'] for c in cards]))
    for subject, status in expected.get('subject_status', {}).items():
        selected = [c for c in cards if subject_of(c) == subject]
        if len(selected) != 1 or selected[0]['task_status'] != status:
            errors.append(f'{subject} 的任务状态应为 {status}')
    details = [d for c in cards for d in c['details']]
    kinds = {d['kind'] for d in details}
    for kind in expected.get('kinds', []):
        if kind not in kinds:
            errors.append('缺少细节类型：' + kind)
    for kind in expected.get('no_active_kinds', []):
        if kind in kinds:
            errors.append('过期细节仍然有效：' + kind)
    descriptions = '\n'.join(d['condition'] + d['description'] for d in details)
    for token in expected.get('contains_current', []):
        if token not in descriptions:
            errors.append('当前细节遗漏：' + token)
    for token in expected.get('absent_current', []):
        if token in descriptions:
            errors.append('已替换细节仍然有效：' + token)
    pending = pending_for_note or any(r.get('action') == 'pending_review' for r in reports)
    if expected.get('pending', False) != pending:
        errors.append('预期待确认' if expected.get('pending') else '不应进入待确认')
    if expected.get('distinct_instances') and (any(not c['instance'] for c in cards) or len({c['instance'] for c in cards}) != len(cards)):
        errors.append('不同轮次未明确区分')
    if 'instance_date_ranges' in expected:
        ranges = [re.findall(r'\d{4}-\d{2}-\d{2}', c['instance'] or '') for c in cards]
        if sorted(ranges) != sorted(expected['instance_date_ranges']):
            errors.append('周期日期不符：' + str(ranges))
    if any(c['task_status'] not in {'todo', 'done'} for c in cards):
        errors.append('产生了第三种任务状态')
    return errors


def saved_pending_by_note(case_dir):
    """Read persisted pending events, including those omitted from service.results."""
    with sqlite3.connect(f'file:{(case_dir / "memory.db").resolve()}?mode=ro', uri=True) as conn:
        items = [json.loads(row[0]) for row in conn.execute(
            "SELECT body FROM memory_workflow_state WHERE kind='pending'")]
    by_note = {}
    for item in items:
        by_note.setdefault(item.get('note', {}).get('id'), []).append(item)
    return by_note


def rescore(cases, output):
    """Audit saved outputs without calling the model or overwriting the original scores."""
    results = []
    for case in cases:
        rows = [json.loads(line) for line in (output / case['id'] / 'steps.jsonl').read_text().splitlines()]
        identities = []
        failures = []
        pending_by_note = saved_pending_by_note(output / case['id'])
        for step, row in zip(case['steps'], rows, strict=True):
            # Keep infrastructure/query/source errors from the original run.
            old_checks = check(row['expected'], row['cards'], identities, row['report'].get('results', []),
                               pending_for_note=bool(row.get('pending_items')))
            errors = [error for error in row['errors'] if error not in old_checks]
            note_id = row['report'].get('note_id', f"{case['id']}-{row['step'] - 1}")
            pending_items = row.get('pending_items', pending_by_note.get(note_id, []))
            errors += check(step['expect'], row['cards'], identities, row['report'].get('results', []),
                            pending_for_note=bool(pending_items))
            if errors:
                failures.append({'step': row['step'], 'text': row['text'], 'errors': errors})
            identities.append([c['id'] for c in row['cards']])
        original = json.loads((output / case['id'] / 'result.json').read_text())
        results.append({**original, 'passed_steps': len(rows) - len(failures), 'failed_steps': failures})
    original_summary = json.loads((output / 'summary.json').read_text())
    summary = {**original_summary, 'scoring_revision': 3,
        'scoring_note': '使用当前数据集断言，并按原消息ID核对数据库中已创建的待确认记录；模型输出及原评分未改写。',
        'passed_steps': sum(r['passed_steps'] for r in results),
        'passed_cases': sum(not r['failed_steps'] for r in results), 'results': results}
    (output / 'rescored-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def run_case(case, run_dir):
    from core import llm_client
    from memory import extractor, repository as repo, task_cards, trace, workflow_state
    from memory.service import process_note_memory, memory_search
    case_dir = Path(run_dir) / case['id']
    case_dir.mkdir()
    repo.DB_PATH = case_dir / 'memory.db'
    trace.TRACE_PATH = case_dir / 'traces.jsonl'
    calls = []
    def real_call(stage):
        def call(**kw):
            started = time.monotonic()
            answer = llm_client.complete_json(**kw)
            calls.append({'stage': stage, 'duration_seconds': round(time.monotonic() - started, 2),
                'input': json.loads(kw['user_prompt']), 'output': answer})
            return answer
        return call
    extractor.complete_json = real_call('extraction')
    task_cards.complete_json = real_call('association')
    previous = []
    identities = []
    rows = []
    base = datetime.fromisoformat('2026-10-20T09:00:00+08:00')
    for index, step in enumerate(case['steps']):
        if step.get('reset_context'):
            previous = []
        ts = step.get('ts') or (base + timedelta(minutes=index)).isoformat()
        note = {'id': f"{case['id']}-{index}", 'space_id': case['id'], 'text': step['text'], 'ts': ts,
            'previous_messages': list(reversed(previous[-3:]))}
        start = len(calls)
        error = None
        try:
            report = process_note_memory(note)
        except Exception as exc:
            error = type(exc).__name__
            report = {'results': [], 'extraction_status': 'failed'}
        records = list(repo.list_memories(case['id'], status='active', memory_type='task', limit=100))
        cards = [task_cards.evidence(r) for r in records if r.memory_key_version == task_cards.VERSION]
        pending_items = [item for item in workflow_state.list_items(case['id'], 'pending')
                         if item.get('note', {}).get('id') == note['id']]
        errors = check(step['expect'], cards, identities, report.get('results', []),
                       pending_for_note=bool(pending_items))
        if error:
            errors.append('处理异常：' + error)
        try:
            hits = memory_search(case['id'], case['query'], memory_type='task', min_score=0, limit=50)
        except Exception as exc:
            hits = []
            errors.append('查询异常：' + type(exc).__name__)
        if cards and not hits:
            errors.append('任务已保存，但主题查询未返回任务')
        for card in cards:
            if any(not d.get('source_ids') or not d.get('sources') for d in card['details']):
                errors.append('细节缺少来源')
        row = {'step': index + 1, 'text': step['text'], 'message_time': ts, 'expected': step['expect'],
            'passed': not errors, 'errors': errors, 'report': report, 'cards': cards,
            'query_hits': hits, 'model_calls': calls[start:], 'pending_items': pending_items}
        rows.append(row)
        with (case_dir / 'steps.jsonl').open('a') as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
        identities.append([c['id'] for c in cards])
        previous.append({'note_id': note['id'], 'role': 'user', 'text': step['text'], 'ts': ts, 'sequence_no': index + 1})
    result = {'id':case['id'], 'name':case['name'], 'steps': len(rows), 'passed_steps': sum(r['passed'] for r in rows),
        'failed_steps': [{'step':r['step'], 'text':r['text'], 'errors':r['errors']} for r in rows if not r['passed']],
        'model_calls':len(calls), 'pending_count':len(workflow_state.list_items(case['id'], 'pending'))}
    (case_dir / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--credentials-env', type=Path)
    parser.add_argument('--data', type=Path, default=Path('eval/data/task_cards_synthetic_v1.json'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cases', nargs='*')
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--rescore', action='store_true', help='Score saved outputs using current expectations; no model calls')
    args = parser.parse_args()
    cases = json.loads(args.data.read_text())['cases']
    if args.cases:
        cases = [c for c in cases if c['id'] in args.cases]
    if args.rescore:
        if args.cases:
            parser.error('--rescore evaluates the entire saved run; omit --cases')
        summary = rescore(cases, args.output)
        raise SystemExit(int(summary['passed_steps'] != summary['total_steps']))
    if args.credentials_env is None:
        parser.error('--credentials-env is required for live model evaluation')
    from dotenv import dotenv_values
    # Only provider credentials/config are imported; never production database or queue settings.
    for key, value in dotenv_values(args.credentials_env).items():
        if value and (key.startswith(('OPENAI_', 'DASHSCOPE_', 'EMBEDDING_')) or key in {
            'SUIXINJI_LLM_API_KEYS', 'SUIXINJI_EMBEDDING_API_KEYS', 'SUIXINJI_FAST_MODEL',
            'SUIXINJI_BALANCED_MODEL', 'SUIXINJI_STRONG_MODEL'}):
            os.environ[key] = value
    os.environ.update({'STORAGE_BACKEND':'local', 'COORDINATION_BACKEND':'local', 'TASK_QUEUE_BACKEND':'local',
        'SUIXINJI_TASK_CARDS_ENABLED':'true', 'SUIXINJI_PREFERENCE_CARDS_ENABLED':'false',
        'SUIXINJI_MEMORY_EXTRACTOR_MODE':'llm', 'SUIXINJI_FAKE_EXTERNALS':'false',
        'SUIXINJI_AGENT_HOOKS_ENABLED':'false', 'SUIXINJI_MEMORY_V3_SHADOW_MODE':'false'})
    from core.config import get_chat_config
    from core.model_router import route_model
    from core.llm_client import _safe_base_url
    route = route_model(task='memory_extraction')
    config = get_chat_config(route.role.value)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'dataset.json').write_text(args.data.read_text())
    metadata = {'model':config.model, 'endpoint':_safe_base_url(config.base_url), 'mode':'real_llm',
        'storage':'isolated_sqlite', 'dataset':str(args.data), 'cases':len(cases),
        'scoring_revision': 3,
        'scope':'真实抽取与关联、保存及查询证据；不含最终自然语言回答或飞书送达'}
    (args.output/'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(metadata, ensure_ascii=False), flush=True)
    results=[]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_case, c, str(args.output.resolve())) for c in cases]
        for future in as_completed(futures):
            result=future.result()
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
    summary={**metadata, 'total_steps':sum(r['steps'] for r in results), 'passed_steps':sum(r['passed_steps'] for r in results),
        'passed_cases':sum(not r['failed_steps'] for r in results), 'results':results}
    (args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k!='results'},ensure_ascii=False),flush=True)
    raise SystemExit(int(summary['passed_steps'] != summary['total_steps']))


if __name__ == '__main__':
    main()
