"""Atomic multi-card projection with optimistic versions and a durable review outbox."""
from contextlib import contextmanager

from core import settings
from core.lifecycle import traced
from memory import repository as repo, workflow_state as store
from memory.models import utc_now_iso
from memory.workflow import stable_id


@contextmanager
def transaction(space_id, snapshots, sources):
    from memory.preference_cards import CardVersionConflict
    if settings.STORAGE_BACKEND == 'postgres':
        from sqlalchemy import select, text
        from infrastructure.database import session_scope
        from infrastructure.schema import Memory, Note
        from repositories.postgres import memory as pg
        from storage.note_storage import is_note_queryable
        with session_scope() as session:
            rows = session.execute(select(Memory).where(Memory.space_id == space_id,
                Memory.id.in_(sorted(snapshots))).order_by(Memory.id).with_for_update()).scalars().all()
            by_id = {r.id: r for r in rows}
            if any(k not in by_id or by_id[k].current_version != r.current_version for k, r in snapshots.items()):
                raise CardVersionConflict('整理期间卡片发生变化，等待重试')
            note_ids = {s['note_id'] for s in sources.values()}
            notes = {n.id: n for n in session.execute(select(Note).where(Note.space_id == space_id,
                Note.id.in_(note_ids)).order_by(Note.id).with_for_update(read=True)).scalars()}
            for source in sources.values():
                note = notes.get(source['note_id'])
                if not note or stable_id(note.text) != source['read_revision'] or not is_note_queryable({'text': note.text, 'sensitivity': note.sensitivity}):
                    raise CardVersionConflict('整理期间原文被修改或删除')
            state = store.State(lambda sql, params: session.execute(text(sql), params), space_id)
            def update(record, values, note_ids):
                for note_id in note_ids:
                    pg._add_source(session, record.id, note_id, 'supported_by')
                pg._versioned_update(session, by_id[record.id], **values, source_note_id=None, source_note_ids=note_ids)
            yield state, update
    else:
        from memory.preference_consolidation import read_source
        # File-backed notes are checked again immediately before the SQLite transaction.
        for source in sources.values():
            current = read_source(space_id, source)
            if current is None or current['read_revision'] != source['read_revision'] or current['note_text'] != source['note_text']:
                raise CardVersionConflict('整理期间原文被修改或删除')
        with repo._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = {k: conn.execute('SELECT * FROM memories WHERE id=? AND space_id=?', (k, space_id)).fetchone() for k in snapshots}
            if any(rows[k] is None or rows[k]['current_version'] != r.current_version for k, r in snapshots.items()):
                raise CardVersionConflict('整理期间卡片发生变化，等待重试')
            state = store.State(conn.execute, space_id)
            def update(record, values, note_ids):
                for note_id in note_ids:
                    repo._add_source_row(conn, record.id, note_id, 'supported_by')
                repo._versioned_update_row(conn, rows[record.id], **values,
                    source_note_id=note_ids[0] if note_ids else None, now=utc_now_iso())
            yield state, update


@traced('偏好整理 · 校验并原子保存', 'database')
def commit(space_id, root, snapshots, changes, sources, plan, pending, job, now):
    from memory.preference_cards import project_card, active_details, CardVersionConflict
    run_id = stable_id(root.id, root.current_version, 'evidence-review-v1')
    written = []
    with transaction(space_id, snapshots, sources) as (state, update):
        if any(state.get('forgotten_source', sid) for sid in sources):
            raise CardVersionConflict('整理期间来源被遗忘')
        for forgotten in state.list('forgotten_source'):
            if any(s['note_id'] == forgotten['note_id'] and forgotten['original_text'] in s['note_text'] for s in sources.values()):
                raise CardVersionConflict('整理上下文含有已遗忘的原文')
        for key, card in changes.items():
            original = snapshots[key]
            if key != root.id and card == original.scope['preference_card']:
                continue
            project_card(card)
            scope = {**original.scope, 'preference_card': card}
            content = f"{card['subject']}关于{card['preference_object']}的偏好：" + '；'.join(d['description'] for d in active_details(card))
            note_ids = sorted({s['note_id'] for d in card['details'] if d['status'] in {'active', 'history'} for s in d['sources']})
            values = {'content': content, 'scope': scope,
                      'polarity': {'喜欢': 'positive', '不喜欢': 'negative', '无所谓': 'neutral'}.get(card['attitude'], 'unknown'),
                      'status': 'archived' if card.get('merged_into') else original.status,
                      'reason': 'preference_evidence_consolidation'}
            update(original, values, note_ids)
            written.append(key)
            latest_job = state.get('card_job', key) or {'id': key, 'generation': 0}
            latest_job.update(dirty=(key != root.id or latest_job.get('generation') != job.get('generation')),
                              force=False)
            if key == root.id:
                latest_job.update(last_run=now.isoformat(), last_related_run=now.isoformat())
            state.put('card_job', key, latest_job)
        state.put('consolidation_run', run_id, {'id': run_id, 'root_id': root.id,
            'before_versions': {k: snapshots[k].current_version for k in written},
            'after_versions': {k: snapshots[k].current_version + 1 for k in written},
            'plan': plan, 'finished_at': now.isoformat()})
        for item in pending:
            state.put('review_outbox', item['id'], item)
    from infrastructure.redis_cache import invalidate_space_cache
    invalidate_space_cache(space_id)
    return {'action': 'consolidated', 'memory_id': root.id, 'run_id': run_id, 'updated_ids': written, 'plan': plan}
