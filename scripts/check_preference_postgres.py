"""Integration check against an isolated local preference_test PostgreSQL database."""
from concurrent.futures import ThreadPoolExecutor
import json
import uuid

from core import settings
from memory import preference_cards as cards, repository as repo, workflow_state as store
from memory.models import MemoryCandidate
from memory.service import process_note_memory, memory_search
from repositories.postgres.notes import save_note


def main():
    from sqlalchemy.engine import make_url
    url = make_url(settings.DATABASE_URL)
    if url.host not in {'127.0.0.1', 'localhost'} or not (url.database or '').startswith('preference_test'):
        raise RuntimeError('Use an isolated local database named preference_test*')
    if settings.STORAGE_BACKEND != 'postgres' or not settings.PREFERENCE_CARDS_ENABLED or settings.MEMORY_EXTRACTOR_MODE != 'rules':
        raise RuntimeError('Enable PostgreSQL, preference cards, and rules mode for this deterministic integration check')
    space = 'preference-it-' + uuid.uuid4().hex[:12]
    sequence = 0
    checks = []

    def new_note(text):
        nonlocal sequence
        sequence += 1
        note = {'id': f'{space}-{sequence}', 'message_id': f'{space}-message-{sequence}', 'space_id': space,
                'text': text, 'ts': f'2026-10-18T10:{sequence:02d}:00+08:00', 'title': '测试',
                'summary': '', 'type': '生活', 'tags': [], 'related': []}
        save_note(note)
        return note

    for text in ['我喜欢咖啡', '我不喜欢早上喝咖啡', '我喜欢晚上喝咖啡']:
        process_note_memory(new_note(text))
    hits = memory_search(space, '咖啡', memory_type='preference')
    assert len(hits) == 1 and len(hits[0]['details']) == 3
    assert all(s.get('message_time') and s.get('original_text') for s in hits[0]['sources'])
    checks.append('write_query_original_time')

    jobs = [new_note('我喜欢低糖咖啡'), new_note('我喜欢高糖咖啡')]
    def concurrent(note):
        candidate = MemoryCandidate('preference', note['text'], .8, .9, evidence_span=note['text'])
        return cards.apply_preference(candidate, note)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(concurrent, jobs))
    assert all(r['action'] in {'insert', 'update'} for r in results)
    record = cards.find_card(space, '用户', '咖啡')
    assert len(cards.active_details(record.scope['preference_card'])) == 5
    checks.append('concurrent_append_no_lost_write')
    cards.run_consolidation(space, force=True)
    assert len(cards.active_details(cards.find_card(space, '用户', '咖啡').scope['preference_card'])) == 5
    assert cards.run_consolidation(space) == []
    checks.append('durable_consolidation_no_loop')

    report = process_note_memory(new_note('他喜欢咖啡'))
    pending = store.list_items(space, 'pending')
    assert report['results'][0]['action'] == 'pending_review' and len(pending) == 1
    assert store.list_items(space, 'notification')
    checks.append('pending_and_notification_persisted')

    process_note_memory(new_note('请忘记我所有咖啡偏好'))
    assert memory_search(space, '咖啡', memory_type='preference') == []
    assert repo.get_memory_timeline(space) == []
    assert '[已忘记的偏好]' in cards.mask_note(space, jobs[0])['text']
    assert concurrent(jobs[0])['action'] == 'discard'
    checks.append('forget_masks_query_timeline_note_and_replay')
    process_note_memory(new_note('我喜欢咖啡'))
    assert len(memory_search(space, '咖啡', memory_type='preference')[0]['details']) == 1
    checks.append('fresh_statement_after_forget')
    print(json.dumps({'space_id': space, 'passed': checks}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
