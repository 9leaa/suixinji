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
    from infrastructure.schema import Task
    with get_engine('lifecycle').connect() as conn:
        run = conn.execute(select(runs).where(runs.c.id==trace_id, runs.c.tenant_id==tenant)).mappings().first()
        if not run:
            return None
        run = dict(run)
        nodes = [dict(r) for r in conn.execute(select(spans).where(spans.c.trace_id==trace_id).order_by(spans.c.started_at, spans.c.id)).mappings()]
        tasks = [dict(r) for r in conn.execute(select(Task.id, Task.task_type, Task.status, Task.attempt_count, Task.last_error, Task.created_at, Task.completed_at).where(Task.tenant_id==tenant, Task.payload_json['_trace']['trace_id'].as_string()==trace_id)).mappings()]
    states = {t['status'] for t in tasks}
    if run['error'] or states & {'dead_letter', 'cancelled'}:
        status='error'
    elif (states and states <= {'completed'} or not states and run['answer'] is not None) and not any(n['status']=='running' for n in nodes):
        status='completed'
    elif states & {'retry'}:
        status='retry'
    else:
        status='running'
    finished = max((n['finished_at'] for n in nodes if n['finished_at']), default=run['created_at'])
    elapsed = ((finished if status in {'completed','error'} else datetime.now(timezone.utc))-run['created_at']).total_seconds()*1000
    return {**run, 'spans':nodes, 'tasks':tasks, 'status':status, 'duration_ms':round(elapsed, 1)}
