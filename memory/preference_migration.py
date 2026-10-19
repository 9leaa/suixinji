"""Explicit, restartable migration of old preferences, using original Notes as evidence."""
from memory import repository as repo, workflow_state as store
from memory.extractor import extract_candidates
from memory.preference_cards import VERSION, apply_preference
from memory.workflow import create_pending


def migrate_legacy(space_id, *, apply=False):
    from storage.note_storage import find_note
    repo.init_db()
    cursor = ''
    results = []
    while True:
        with store.transaction(space_id) as state:
            rows = state.execute('SELECT id FROM memories WHERE space_id=:space AND memory_type=:type '
                                 'AND memory_key_version<>:version AND status=:status AND id>:cursor ORDER BY id LIMIT 100',
                                 {'space': space_id, 'type': 'preference', 'version': VERSION,
                                  'status': 'active', 'cursor': cursor}).fetchall()
        if not rows:
            break
        for row in rows:
            record = repo.get_memory(row[0])
            report = {'memory_id': record.id, 'source_ids': [s.note_id for s in record.sources], 'status': 'planned'}
            if apply:
                targets = set()
                unresolved = False
                for source in record.sources:
                    note = find_note(space_id, source.note_id)
                    if not note:
                        unresolved = True
                        create_pending(space_id, {'id': source.note_id, 'text': record.content, 'space_id': space_id},
                                       '旧记忆缺少可核验的笔记原文，未自动迁移', code='migration_source_missing', target_ids=[record.id])
                        continue
                    note = {**note, 'user_id': record.scope.get('owner_id') or 'owner'}
                    preferences = [c for c in extract_candidates(note['id'], note['text']) if c.memory_type == 'preference']
                    if not preferences:
                        unresolved = True
                        create_pending(space_id, note, '旧记忆未能从原文重新提取偏好', code='migration_extraction', target_ids=[record.id])
                    for candidate in preferences:
                        result = apply_preference(candidate, note)
                        if result.get('memory_id'):
                            targets.add(result['memory_id'])
                        if result['action'] == 'pending_review':
                            unresolved = True
                if not record.sources:
                    unresolved = True
                    create_pending(space_id, {'id': record.id, 'text': record.content, 'space_id': space_id},
                                   '旧偏好没有来源笔记，未自动迁移', code='migration_source_missing', target_ids=[record.id])
                if not unresolved:
                    store.put(space_id, 'migrated_preference', record.id, {'id': record.id, 'card_ids': sorted(targets)})
                    repo.update_memory(record.id, status='archived', reason='preference_card_migration')
                report.update(status='pending' if unresolved else 'migrated', card_ids=sorted(targets))
            results.append(report)
        cursor = rows[-1][0]
    return results
