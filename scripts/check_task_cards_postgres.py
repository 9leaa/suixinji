"""Task-card integration checks; only an isolated local task_cards_test* database is accepted."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import uuid
from unittest.mock import patch

from core import settings
from memory import repository as repo, task_cards as cards, workflow_state
from memory.service import memory_search
from repositories.postgres.notes import save_note


def main():
    from sqlalchemy.engine import make_url
    url = make_url(settings.DATABASE_URL)
    if url.host not in {'127.0.0.1', 'localhost'} or not (url.database or '').startswith('task_cards_test'):
        raise RuntimeError('Use an isolated local database named task_cards_test*')
    if settings.STORAGE_BACKEND != 'postgres' or not settings.TASK_CARDS_ENABLED:
        raise RuntimeError('Enable PostgreSQL and task cards')
    space = 'task-card-it-' + uuid.uuid4().hex[:12]
    checks = []
    sequence = 0

    def note(text):
        nonlocal sequence
        sequence += 1
        value = {'id': f'{space}-{sequence}', 'message_id': f'{space}-m-{sequence}', 'space_id': space,
            'text': text, 'ts': '2026-10-19T15:00:00+08:00', 'title': '任务卡片测试', 'summary': '',
            'type': '工作', 'tags': [], 'related': []}
        save_note(value)
        return value

    def candidate(source, **kw):
        data = {'subject': '用户', 'task': '提交项目周报', 'instance': '第43周', 'task_status': 'todo',
            'details': [], 'evidence_span': source['text'], **kw}
        return cards.candidate_from_row(source['id'], source['text'], data)

    first_note = note('第43周提交项目周报')
    first_candidate = candidate(first_note)
    first = cards.apply_task(first_candidate, first_note)
    assert first['action'] == 'insert', first
    card_id = first['memory_id']
    second_note = note('第44周提交项目周报')
    second = cards.apply_task(candidate(second_note, instance='第44周'), second_note)
    assert second['memory_id'] != card_id
    hits = memory_search(space, '项目周报', memory_type='task', min_score=0)
    assert {hit['instance'] for hit in hits} == {'第43周', '第44周'}
    assert all(hit['sources'][0]['message_time'] == first_note['ts'] for hit in hits)
    checks.append('write_search_instances_and_original_message_time')

    gate = threading.Barrier(2)
    seen = set()
    lock = threading.Lock()
    def association(**kwargs):
        thread_id = threading.get_ident()
        with lock:
            first_read = thread_id not in seen
            seen.add(thread_id)
        if first_read:
            gate.wait(timeout=10)
        return {'action': 'update', 'target_id': card_id}
    jobs = [note('收入数据已核对'), note('客户数据已核对')]
    def append(source):
        detail = {'kind': 'progress', 'condition': '', 'description': source['text'], 'evidence_span': source['text']}
        return cards.apply_task(candidate(source, details=[detail]), source)
    with patch.object(cards, 'complete_json', association):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(append, jobs))
    assert all(r['action'] == 'update_task' for r in results), results
    current = repo.get_memory(card_id)
    assert len(cards.active_details(current.scope['task_card'])) == 2 and current.current_version == 3
    checks.append('concurrent_progress_keeps_both_changes')

    ended_note = note('第43周的周报取消了')
    ended_candidate = candidate(ended_note, task_status='done', closure_reason='cancelled')
    ended = cards.apply_task(ended_candidate, ended_note)
    assert ended['task_card']['closure_reason'] == 'cancelled' and '已完成' not in ended['task_card']['content']
    assert repo.get_memory(second['memory_id']).status == 'active'
    checks.append('cancel_is_done_without_archiving_other_instance')

    result_note = note('取消原因是本周无需汇报')
    with patch.object(cards, 'complete_json', return_value={'action': 'update', 'target_id': card_id}):
        extra = cards.apply_task(candidate(result_note, task_status='done', details=[
            {'kind': 'outcome', 'description': result_note['text'], 'evidence_span': result_note['text']}]), result_note)
    assert extra['task_card']['closure_reason'] == 'cancelled'
    assert len(extra['task_card']['details']) == 3
    checks.append('closed_task_result_is_not_discarded')

    before = repo.get_memory(card_id).current_version
    replay = cards.apply_task(ended_candidate, ended_note)
    assert replay['idempotent'] and repo.get_memory(card_id).current_version == before
    checks.append('replay_does_not_roll_back_newer_card')

    ambiguous_note = note('周报完成了')
    ambiguous = cards.apply_task(candidate(ambiguous_note, instance=None, task_status='done'), ambiguous_note)
    assert ambiguous['action'] == 'pending_review'
    assert workflow_state.list_items(space, 'pending') and workflow_state.list_items(space, 'notification')
    checks.append('ambiguous_instance_has_durable_pending')

    timeline = repo.get_memory_timeline(space, memory_id=card_id)
    assert len(timeline[0]['versions']) >= 4 and timeline[0]['closure_reason'] == 'cancelled'
    checks.append('postgres_timeline_keeps_task_structure_and_versions')
    print(json.dumps({'space_id': space, 'passed': checks}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
