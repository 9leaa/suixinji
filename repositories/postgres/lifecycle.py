"""Console persistence shares the existing engine, using additive tables only."""
from datetime import datetime, timezone
from sqlalchemy import MetaData, Table, Column, String, Text, DateTime, Float, JSON, Index, select, update
from sqlalchemy.dialects.postgresql import insert
from infrastructure.database import get_engine

metadata = MetaData()
runs = Table('lifecycle_runs', metadata,
    Column('id', String(64), primary_key=True), Column('tenant_id', String(255), nullable=False),
    Column('space_id', String(255), nullable=False), Column('message_id', String(255), nullable=False),
    Column('mode', String(20), nullable=False), Column('text', Text, nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False), Column('task_id', String(255)),
    Column('answer', Text), Column('error', Text), Column('config_json', JSON, nullable=False))
Index('ix_lifecycle_runs_tenant_created', runs.c.tenant_id, runs.c.created_at)
spans = Table('lifecycle_spans', metadata,
    Column('id', String(64), primary_key=True), Column('trace_id', String(64), nullable=False),
    Column('parent_id', String(64)), Column('name', String(255)), Column('kind', String(40)),
    Column('started_at', DateTime(timezone=True)), Column('finished_at', DateTime(timezone=True)),
    Column('duration_ms', Float), Column('status', String(40)), Column('input_json', JSON),
    Column('output_json', JSON), Column('metadata_json', JSON), Column('error', Text))
Index('ix_lifecycle_spans_trace_started', spans.c.trace_id, spans.c.started_at)


def initialize():
    metadata.create_all(get_engine('lifecycle'))


def create_run(**values):
    with get_engine('lifecycle').begin() as conn:
        conn.execute(runs.insert().values(created_at=datetime.now(timezone.utc), **values))


def update_run(trace_id, **values):
    from core.lifecycle import safe
    with get_engine('lifecycle').begin() as conn:
        conn.execute(update(runs).where(runs.c.id==trace_id).values(**safe(values)))


def save_span(row):
    with get_engine('lifecycle').begin() as conn:
        conn.execute(insert(spans).values(**row).on_conflict_do_update(index_elements=['id'], set_={k:v for k,v in row.items() if k!='id'}))


def list_runs(tenant, limit=50):
    with get_engine('lifecycle').connect() as conn:
        return [dict(r) for r in conn.execute(select(runs).where(runs.c.tenant_id==tenant).order_by(runs.c.created_at.desc()).limit(limit)).mappings()]


def detail(trace_id, tenant):
    from infrastructure.schema import Task, TaskAttempt
    with get_engine('lifecycle').connect() as conn:
        run = conn.execute(select(runs).where(runs.c.id==trace_id, runs.c.tenant_id==tenant)).mappings().first()
        if not run:
            return None
        run = dict(run)
        nodes = [dict(r) for r in conn.execute(select(spans).where(spans.c.trace_id==trace_id).order_by(spans.c.started_at, spans.c.id)).mappings()]
        tasks = [dict(r) for r in conn.execute(select(Task.id, Task.task_type, Task.status, Task.attempt_count, Task.last_error, Task.created_at, Task.completed_at).where(Task.tenant_id==tenant, Task.payload_json['_trace']['trace_id'].as_string()==trace_id)).mappings()]
        attempts = [dict(r) for r in conn.execute(select(TaskAttempt.task_id, TaskAttempt.attempt_no, TaskAttempt.status, TaskAttempt.started_at, TaskAttempt.finished_at).where(TaskAttempt.task_id.in_([t['id'] for t in tasks]))).mappings()] if tasks else []
    return project_detail(run, nodes, tasks, attempts)


def project_detail(run, nodes, tasks, attempts):
    # Read-time reconciliation preserves raw telemetry and also fixes old runs.
    nodes = [dict(n) for n in nodes]
    by_id = {n['id']: n for n in nodes}
    owners = {}
    for attempt in sorted(attempts, key=lambda a: a['started_at']):
        workers = [n for n in nodes if n.get('kind') == 'worker'
                   and (n.get('input_json') or {}).get('task_id') == attempt['task_id']
                   and n['started_at'] <= attempt['started_at']]
        if workers:
            owner = max(workers, key=lambda n: n['started_at'])
            owners.setdefault(owner['id'], attempt)
    for node in nodes:
        if node['status'] != 'running':
            continue
        owner = node
        while owner and owner.get('kind') != 'worker':
            owner = by_id.get(owner.get('parent_id'))
        attempt = owners.get(owner['id']) if owner else None
        if attempt and attempt['finished_at'] is not None:
            node['status'] = 'interrupted'
            node['metadata_json'] = {**(node.get('metadata_json') or {}),
                'original_status': 'running', 'attempt_status': attempt['status'],
                'attempt_no': attempt['attempt_no'], 'reconciled': True,
                'explanation': '所属任务尝试已结束，但该节点未记录结束；不代表仍在执行。实际中断时间及耗时未知。'}
    states = {t['status'] for t in tasks}
    if run['error'] or states & {'dead_letter', 'cancelled'}:
        status='error'
    elif states and states <= {'completed'} or not states and run['answer'] is not None and not any(n['status']=='running' for n in nodes):
        status='completed'
    elif states & {'retry'}:
        status='retry'
    else:
        status='running'
    finished = max([n['finished_at'] for n in nodes if n['finished_at']]
                   + [t['completed_at'] for t in tasks if t['completed_at']], default=run['created_at'])
    elapsed = ((finished if status in {'completed','error'} else datetime.now(timezone.utc))-run['created_at']).total_seconds()*1000
    return {**run, 'spans':nodes, 'tasks':tasks, 'status':status, 'duration_ms':round(elapsed, 1)}


def search_runs(tenant, *, space='', query='', status='', since=None, until=None, offset=0, limit=30):
    """Filter and page at the database; status follows the linked task's current state."""
    from sqlalchemy import case, exists, func, and_, or_
    from infrastructure.schema import Task
    linked = Task.payload_json['_trace']['trace_id'].astext == runs.c.id
    def has(states=None):
        terms = [Task.tenant_id == tenant, linked]
        if states:
            terms.append(Task.status.in_(states))
        return exists(select(Task.id).where(*terms))
    unfinished = exists(select(Task.id).where(Task.tenant_id == tenant, linked, Task.status != 'completed'))
    live_span = exists(select(spans.c.id).where(spans.c.trace_id == runs.c.id, spans.c.status == 'running'))
    projected = case(
        (or_(runs.c.error.is_not(None), has(['dead_letter', 'cancelled'])), 'error'),
        (or_(and_(has(), ~unfinished),
             and_(~has(), runs.c.answer.is_not(None), ~live_span)), 'completed'),
        (has(['retry']), 'retry'),
        else_='running').label('status')
    statement = select(runs, projected).where(runs.c.tenant_id == tenant)
    if space:
        statement = statement.where(runs.c.space_id == space)
    if query:
        statement = statement.where(runs.c.text.icontains(query, autoescape=True))
    if since:
        statement = statement.where(runs.c.created_at >= since)
    if until:
        statement = statement.where(runs.c.created_at <= until)
    if status:
        statement = statement.where(projected == status)
    with get_engine('lifecycle').connect() as conn:
        total = conn.scalar(select(func.count()).select_from(statement.subquery()))
        items = [dict(r) for r in conn.execute(statement.order_by(runs.c.created_at.desc(), runs.c.id.desc())
                 .offset(offset).limit(limit)).mappings()]
    return {'items': items, 'total': total, 'offset': offset, 'limit': limit}
