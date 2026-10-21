"""Schedule preference maintenance only when useful, with one outstanding job per space."""
from datetime import datetime, timedelta, timezone
import hashlib

from core import settings
from memory import workflow_state as store
from memory.preference_cards import all_cards, active_details
from memory.scheduler import is_evaluation_space


def excluded(space):
    return is_evaluation_space(space) or space.startswith(("sample30-", "task-deploy-smoke-"))


def cards_due(records, jobs, now):
    total = sum(max(0, r.scope['preference_card']['changes'] - r.scope['preference_card'].get('last_consolidated_changes', 0)) for r in records)
    for r in records:
        card = r.scope['preference_card']
        if r.status != 'active' or card.get('merged_into'):
            continue
        job = jobs.get(r.id, {})
        age = (now - datetime.fromisoformat(job.get('last_run') or r.created_at)).total_seconds()
        related = (now - datetime.fromisoformat(job.get('last_related_run') or r.created_at)).total_seconds()
        changed = card['changes'] > card.get('last_consolidated_changes', 0)
        refresh = related >= settings.PREFERENCE_CARD_RELATED_SECONDS
        if (job.get('dirty') or changed or refresh) and (
            job.get('force') or len(active_details(card)) >= settings.PREFERENCE_CARD_SOFT_LIMIT
            or total >= settings.PREFERENCE_CARD_CHANGE_LIMIT
            or age >= settings.PREFERENCE_CARD_INTERVAL_SECONDS or refresh
        ):
            return True
    return False


def enqueue_due(space, *, now=None):
    if excluded(space):
        return None
    now = now or datetime.now(timezone.utc)
    from sqlalchemy import select, text
    from infrastructure.database import session_scope
    from infrastructure.schema import Task
    from repositories.postgres.dispatch import _enqueue_task_in_session
    from repositories.postgres.common import DEFAULT_TENANT_ID
    # Transaction-scoped lock makes check + insert atomic across scheduler processes.
    lock = int.from_bytes(hashlib.sha256(('preference-maintenance:' + space).encode()).digest()[:8], 'big') % 2**63
    with session_scope() as session:
        session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock})
        condition = (Task.space_id == space, Task.tenant_id == DEFAULT_TENANT_ID,
                     Task.task_type == 'memory', Task.payload_json['operation'].astext == 'preference_maintenance')
        outstanding = session.execute(select(Task.id).where(*condition, Task.status.in_(['queued','running','retry','blocked'])).limit(1)).scalar_one_or_none()
        if outstanding:
            return None
        latest = session.execute(select(Task.completed_at, Task.created_at).where(*condition).order_by(Task.created_at.desc()).limit(1)).first()
        # Failed per-card reviews must not turn into a new model call every minute.
        if latest and now - (latest.completed_at or latest.created_at) < timedelta(hours=1):
            return None
        records = list(all_cards(space))
        jobs = {j['id']: j for j in store.list_items(space, 'card_job')}
        due = cards_due(records, jobs, now)
        digest_due = (not latest or (latest.completed_at or latest.created_at).date() < now.date()) and (
            any(x.get('status') == 'pending' for x in store.list_items(space, 'pending'))
            or any(x.get('status') == 'suspected' for x in store.list_items(space, 'issue')))
        if not due and not digest_due:
            return None
        return _enqueue_task_in_session(session, task_type='memory', tenant_id=DEFAULT_TENANT_ID,
            space_id=space, source_message_id=None,
            idempotency_key=f'preference-maintenance-due:{space}:{now.isoformat()}',
            payload={'operation':'preference_maintenance'}, priority=-1)
