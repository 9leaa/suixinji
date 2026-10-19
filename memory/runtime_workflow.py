"""Adapters between durable memory workflow state and existing delivery workers."""
from core import settings
from memory import workflow_state as store
from memory.models import utc_now_iso
from memory.workflow import create_pending, capture_issue, deliver_notifications, resolve_pending


def register_route(space_id, note_id, *, chat_id, user_id=None, trace_id=None, tenant_id='default'):
    if chat_id:
        store.put(space_id, 'reply_route', note_id, {'chat_id': chat_id, 'user_id': user_id, 'trace_id': trace_id, 'tenant_id': tenant_id})


def flush_notifications(space_id):
    def sender(key, text, note_id):
        route = store.get(space_id, 'reply_route', note_id)
        if not route:
            return False
        chat_id = route['chat_id']
        if chat_id.startswith('console:'):
            from repositories.postgres.lifecycle import update_run
            if not route.get('trace_id'):
                raise ValueError('console notification trace is not yet available')
            update_run(route['trace_id'], answer=text)
        elif settings.STORAGE_BACKEND == 'postgres':
            from repositories.postgres.dispatch import enqueue_task
            enqueue_task(task_type='delivery', space_id=space_id, tenant_id=route.get('tenant_id') or 'default',
                         idempotency_key=f'memory-notification:{space_id}:{key}',
                         payload={'chat_id': chat_id, 'text': text, 'delivery_key': f'memory-notification:{space_id}:{key}',
                                  'delivery_type': 'memory_notification'})
        else:
            from bot.feishu_bot import send_text
            from runtime.delivery_store import get_delivery, reserve_delivery, mark_sent, mark_failed
            delivery_key = f'memory-notification:{space_id}:{key}'
            previous = get_delivery(delivery_key)
            if previous and previous.status == 'sent':
                return
            if not reserve_delivery(delivery_key, delivery_type='memory_notification', space_id=space_id):
                raise RuntimeError('notification delivery is already in progress')
            try:
                if send_text(chat_id, text) is False:
                    raise RuntimeError('notification send failed')
                mark_sent(delivery_key)
            except Exception as error:
                mark_failed(delivery_key, type(error).__name__)
                raise
    return deliver_notifications(space_id, sender)


def daily_issue_digest(space_id, *, day=None):
    day = day or utc_now_iso()[:10]
    pending = [r for r in store.list_items(space_id, 'pending') if r['status'] == 'pending']
    issues = [r for r in store.list_items(space_id, 'issue') if r['status'] == 'suspected']
    if not pending and not issues:
        return
    # Each source owner receives only the cases associated with their own route.
    routes = {r['id']: r['note']['id'] for r in pending}
    routes.update({r['id']: r['snapshot'].get('note', {}).get('id') for r in issues})
    by_destination = {}
    for key, note_id in routes.items():
        route = store.get(space_id, 'reply_route', note_id) if note_id else None
        if route:
            by_destination.setdefault(route['chat_id'], []).append((key, note_id))
    from memory.workflow import stable_id
    for chat_id, entries in by_destination.items():
        key = 'digest:' + stable_id(day, chat_id)
        with store.transaction(space_id) as state:
            if state.get('notification', key):
                continue
            seen = state.get('digest_seen', chat_id) or {'case_ids': []}
            new_entries = [entry for entry in entries if entry[0] not in seen['case_ids']]
            if not new_entries:
                continue
            state.put('notification', key, {'id': key, 'note_id': entries[0][1], 'status': 'queued',
                'text': f'记忆质量日报 {day}：新增 {len(new_entries)} 项待处理或待复核。\n' + '\n'.join(x[0] for x in new_entries[:10])})
            state.put('digest_seen', chat_id, {'case_ids': sorted(set(seen['case_ids']) | {entry[0] for entry in new_entries})})


def guarded_handler(handler):
    """All user task types expose unrecoverable or retrying processing to the user."""
    from functools import wraps
    @wraps(handler)
    def run(task):
        if not settings.PREFERENCE_CARDS_ENABLED:
            return handler(task)
        from repositories.postgres.dispatch import load_inbox_record
        from core.lifecycle import current
        payload = task.get('payload_json') or {}
        inbox_id = payload.get('inbox_id') or payload.get('barrier_inbox_id')
        inbox = load_inbox_record(inbox_id) if inbox_id else None
        note_id = payload.get('note_id') or (inbox or {}).get('id') or task['id']
        space_id = task['space_id']
        context = current() or {}
        route = store.get(space_id, 'reply_route', note_id)
        if not route:
            register_route(space_id, note_id, chat_id=payload.get('chat_id') or (inbox or {}).get('chat_id'),
                           trace_id=context.get('trace_id'), tenant_id=task.get('tenant_id') or 'default')
        try:
            result = handler(task)
            for pending in store.list_items(space_id, 'pending'):
                if pending['code'] == 'task_failed' and pending['status'] == 'pending' and pending['target_ids'] == [task['id']]:
                    resolve_pending(space_id, pending['id'], result='重试成功，消息已处理')
            return result
        except Exception as error:
            # Scheduling deferrals are expected, not message failures.
            from runtime.streams.worker import RetryLater
            if isinstance(error, RetryLater):
                raise
            note = {'id': note_id, 'space_id': space_id,
                    'text': (inbox or {}).get('text') or payload.get('question') or '',
                    'ts': (inbox or {}).get('ts'), 'user_id': payload.get('user_id') or next(
                        ((inbox or {}).get('sender', {}).get(key) for key in ('user_id', 'open_id', 'union_id', 'source_user_id', 'id')
                         if (inbox or {}).get('sender', {}).get(key)), 'owner')}
            create_pending(space_id, note, f"{task['task_type']} 处理失败，正在重试（{type(error).__name__}）",
                           code='task_failed', stage=task['task_type'], target_ids=[task['id']])
            capture_issue(space_id, stage=task['task_type'], reason=type(error).__name__,
                          snapshot={'note': note, 'task_id': task['id'], 'error': str(error)}, trace_id=context.get('trace_id'))
            raise
        finally:
            try:
                flush_notifications(space_id)
            except Exception:
                import logging
                logging.getLogger(__name__).warning('Memory notifications remain queued', exc_info=True)
    return run
