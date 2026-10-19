"""User-visible pending lifecycle and reviewable quality cases; no network sends here."""
import hashlib
import json
from pathlib import Path
from memory import workflow_state as store
from memory.models import utc_now_iso


def stable_id(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]


def create_pending(space_id, note, reason, *, code='processing_failed', stage='memory', candidate=None, target_ids=None):
    key = stable_id(note.get('id'), note.get('text'), stage, code, candidate)
    with store.transaction(space_id) as state:
        prior = state.get('pending', key)
        if prior:
            return prior
        row = {'id': key, 'space_id': space_id, 'note': note, 'reason': reason, 'code': code,
               'stage': stage, 'candidate': candidate, 'target_ids': target_ids or [],
               'status': 'pending', 'created_at': utc_now_iso()}
        state.put('pending', key, row)
        message = f"这条消息有内容待处理 [pending:{key}]\n原文：{note.get('text', '')}\n原因：{reason}"
        message += f'\n补充：/pending resolve {key} 补充说明\n取消：/pending cancel {key}'
        state.put('notification', key, {'id': key, 'text': message, 'note_id': note.get('id'),
                                      'status': 'queued', 'created_at': utc_now_iso()})
    capture_issue(space_id, stage=stage, reason=code,
                  snapshot={'note': note, 'candidate': candidate, 'target_ids': target_ids or [], 'pending_id': key})
    return row


def resolve_pending(space_id, key, *, result, cancelled=False):
    with store.transaction(space_id) as state:
        row = state.get('pending', key)
        if row is None or row['status'] != 'pending':
            return False
        row.update(status='cancelled' if cancelled else 'resolved', result=result, resolved_at=utc_now_iso())
        state.put('pending', key, row)
        notification_id = key + ':resolved'
        state.put('notification', notification_id, {'id': notification_id, 'note_id': row['note'].get('id'),
                  'status': 'queued', 'text': f"待处理事项已{'取消' if cancelled else '解决'} [pending:{key}]\n{result}",
                  'created_at': utc_now_iso()})
        return True


def deliver_notifications(space_id, sender):
    """Sender must accept a stable idempotency key and durably queue delivery."""
    count = 0
    for notification in store.list_items(space_id, 'notification'):
        if notification['status'] != 'queued':
            continue
        if sender(notification['id'], notification['text'], notification.get('note_id')) is False:
            continue
        with store.transaction(space_id) as state:
            current = state.get('notification', notification['id'])
            current['status'] = 'enqueued'
            state.put('notification', notification['id'], current)
        count += 1
    return count


def capture_issue(space_id, *, stage, reason, snapshot, trace_id=None):
    key = stable_id(stage, reason, snapshot)
    with store.transaction(space_id) as state:
        prior = state.get('issue', key)
        if prior:
            return prior
        row = {'id': key, 'space_id': space_id, 'stage': stage, 'reason': reason,
               'snapshot': snapshot, 'trace_id': trace_id, 'status': 'suspected',
               'expected': None, 'created_at': utc_now_iso(), 'schema_version': 'preference-card-v1'}
        from core import settings
        from memory.preference_cards import EXTRACTION_PROMPT, DECISION_PROMPT
        from memory.prompts import MEMORY_CARD_EXTRACTOR_PROMPT
        from memory.preference_consolidation import REVIEW_PROMPT, RELATION_PROMPT, SUPPORT_PROMPT
        row.update(extractor_mode=settings.MEMORY_EXTRACTOR_MODE,
                   prompt_hash=stable_id(MEMORY_CARD_EXTRACTOR_PROMPT, EXTRACTION_PROMPT, DECISION_PROMPT,
                                         REVIEW_PROMPT, RELATION_PROMPT, SUPPORT_PROMPT),
                   dataset_type={'association': 'association', 'consolidation': 'consolidation',
                                 'query': 'query', 'user_feedback': 'query'}.get(stage, 'extraction'),
                   group_id=stable_id(space_id))
        if (snapshot.get('candidate') or {}).get('memory_type') == 'task':
            from memory.task_schema import TASK_EXTRACTION_PROMPT
            from memory.task_cards import DECISION_PROMPT as TASK_DECISION_PROMPT
            row.update(schema_version='task-card-v1', prompt_hash=stable_id(TASK_EXTRACTION_PROMPT, TASK_DECISION_PROMPT))
        state.put('issue', key, row)
        return row


def review_issue(space_id, key, *, status, expected=None, reviewer):
    if status not in {'confirmed', 'normal', 'insufficient'}:
        raise ValueError('invalid review status')
    if status == 'confirmed' and not expected:
        raise ValueError('confirmed cases require a reviewed expected result')
    with store.transaction(space_id) as state:
        row = state.get('issue', key)
        if row is None:
            raise KeyError(key)
        row.update(status=status, expected=expected, reviewer=reviewer, reviewed_at=utc_now_iso())
        state.put('issue', key, row)
    return row


def export_cases(space_id, path, *, replacements=None):
    """Private export by default; explicit stable replacements support shared datasets."""
    rows = [r for r in store.list_items(space_id, 'issue') if r['status'] == 'confirmed']
    rows = [{**r, 'split': 'test' if int(r.get('group_id', stable_id(space_id))[:8], 16) % 5 == 0 else 'development'} for r in rows]
    def redact(value):
        if isinstance(value, dict):
            return {k: redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [redact(v) for v in value]
        if isinstance(value, str):
            for old, new in sorted((replacements or {}).items(), key=lambda item: len(item[0]), reverse=True):
                value = value.replace(old, new)
        return value
    content = '\n'.join(json.dumps(redact(r), ensure_ascii=False) for r in rows)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content + ('\n' if rows else ''), encoding='utf-8')
    destination.chmod(0o600)
    return len(rows)
