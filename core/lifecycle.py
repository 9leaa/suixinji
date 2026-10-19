"""Opt-in durable spans for the message console; no context means no work."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from functools import wraps
import inspect
import logging
import time
import uuid
from enum import Enum
from decimal import Decimal
from typing import Any

_CURRENT = ContextVar('lifecycle', default=None)
_OBSERVATION = ContextVar('lifecycle_observation', default=None)
LOG = logging.getLogger(__name__)
SECRET_KEYS = {'api_key', 'authorization', 'password', 'secret', 'token', 'access_token', 'refresh_token'}


def safe(value: Any, depth=0):
    if depth > 18:
        return {'omitted': 'depth limit'}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        from core.sensitive import redact_sensitive_text
        return redact_sensitive_text(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    elif hasattr(value, 'model_dump'):
        value = value.model_dump(mode='json')
    elif hasattr(type(value), '__table__'):
        value = {c.name: getattr(value, c.name) for c in value.__table__.columns if c.name not in {'embedding'}}
    if isinstance(value, dict):
        return {str(k): '[redacted]' if str(k).lower() in SECRET_KEYS or str(k).lower().endswith('_token') or 'api_key' in str(k).lower() else safe(v, depth+1) for k,v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [safe(v, depth+1) for v in value]
    return f'<{type(value).__name__}>'


def current():
    return dict(_CURRENT.get() or {})


@contextmanager
def resume(context):
    token = _CURRENT.set(dict(context) if context and context.get('trace_id') else None)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def _save(row):
    try:
        from repositories.postgres.lifecycle import save_span
        save_span(row)
    except Exception:
        LOG.exception('lifecycle span persistence failed')


@contextmanager
def span(name, kind='stage', inputs=None, **metadata):
    ctx = current()
    data = {'output': None, 'metadata': metadata, 'status': 'completed'}
    if not ctx:
        yield data
        return
    start = time.perf_counter()
    row = {'id': uuid.uuid4().hex, 'trace_id': ctx['trace_id'], 'parent_id': ctx.get('span_id'),
           'name': name, 'kind': kind, 'started_at': datetime.now(timezone.utc), 'finished_at': None,
           'duration_ms': None, 'status': 'running', 'input_json': safe(inputs), 'output_json': None,
           'metadata_json': safe(metadata), 'error': None}
    _save(row)
    token = _CURRENT.set({**ctx, 'span_id': row['id']})
    observation_token = _OBSERVATION.set(data)
    try:
        yield data
    except BaseException as exc:
        data['status'] = 'error'
        row['error'] = safe(f'{type(exc).__name__}: {exc}')
        raise
    finally:
        _CURRENT.reset(token)
        _OBSERVATION.reset(observation_token)
        row.update(finished_at=datetime.now(timezone.utc), duration_ms=round((time.perf_counter()-start)*1000, 3),
                   status=data['status'], output_json=safe(data['output']), metadata_json=safe(data['metadata']))
        _save(row)


def traced(name, kind='stage', exclude=(), output_arg=None):
    def decorate(fn):
        signature = inspect.signature(fn)
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not _CURRENT.get():
                return fn(*args, **kwargs)
            bound = signature.bind(*args, **kwargs).arguments
            inputs = {k:v for k,v in bound.items() if k not in {'self', 'session', 'client', 'trace', 'hook_context', *exclude}}
            with span(name, kind, inputs) as observation:
                result = fn(*args, **kwargs)
                observation['output'] = bound.get(output_arg) if output_arg else result
                return result
        return wrapped
    return decorate


def event(name, inputs=None, output=None, status='completed', **metadata):
    ctx = current()
    if not ctx:
        return
    now = datetime.now(timezone.utc)
    _save(dict(id=uuid.uuid4().hex, trace_id=ctx['trace_id'], parent_id=ctx.get('span_id'), name=name,
               kind='event', started_at=now, finished_at=now, duration_ms=None, status=status,
               input_json=safe(inputs), output_json=safe(output), metadata_json=safe(metadata), error=None))


def submit_with_context(pool, fn, *args, **kwargs):
    return pool.submit(copy_context().run, fn, *args, **kwargs)


def trace_message(fn):
    @wraps(fn)
    def wrapped(self, message):
        fields = message.fields
        ctx = {'trace_id': fields.get('trace_id'), 'span_id': fields.get('parent_span_id')}
        if not ctx['trace_id']:
            return fn(self, message)
        with resume(ctx):
            try:
                published=fields.get('published_at') or datetime.fromtimestamp(int(message.message_id.split('-')[0])/1000, timezone.utc).isoformat()
            except (ValueError, TypeError):
                published=None
            ctx=wait_node('Redis · 队列等待', published, {'stream':message.stream, 'redis_message_id':message.message_id})
        with resume(ctx), span('Worker · '+self.task_type, 'worker', fields) as observation:
            result = fn(self, message)
            from repositories.postgres.tasks import get_task
            task = get_task(str(fields.get('task_id')))
            observation['output'] = task
            state = (task or {}).get('status')
            observation['status'] = {'completed':'completed', 'retry':'retry', 'dead_letter':'error'}.get(state, 'waiting')
            return result
    return wrapped


def recall_channel(name, query, call):
    with span('召回 · '+name, 'retrieval', {'query':query, 'channel':name}) as observation:
        result = call()
        observation['output'] = {'count':len(result), 'candidates':result}
        return result


def wait_node(name, started_at, metadata=None):
    """Measured queue residence from persisted/Redis timestamps, not execution CPU time."""
    ctx=current()
    if not ctx or not started_at:
        return ctx
    try:
        started = datetime.fromisoformat(str(started_at))
        if started.tzinfo is None:
            started=started.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return ctx
    finished=datetime.now(timezone.utc)
    ident=uuid.uuid4().hex
    _save(dict(id=ident,trace_id=ctx['trace_id'],parent_id=ctx.get('span_id'),name=name,kind='queue',
               started_at=started,finished_at=finished,duration_ms=max(0,(finished-started).total_seconds()*1000),
               status='completed',input_json=None,output_json=None,
               metadata_json=safe({'timing':'wall clock residence; requires synchronized hosts',**(metadata or {})}),error=None))
    return {**ctx,'span_id':ident}


def annotate(**metadata):
    observation = _OBSERVATION.get()
    if observation is not None:
        observation["metadata"].update(metadata)
