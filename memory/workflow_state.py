"""Durable pending, notification, quality and consolidation state on both backends."""
from contextlib import contextmanager
import hashlib
import json
from sqlalchemy import Column, MetaData, String, Table, Text, text
from core import settings

metadata = MetaData()
state_table = Table(
    'memory_workflow_state', metadata,
    Column('space_id', String(255), primary_key=True),
    Column('kind', String(64), primary_key=True),
    Column('id', String(255), primary_key=True),
    Column('body', Text, nullable=False),
)


class State:
    def __init__(self, execute, space_id):
        self.execute = execute
        self.space_id = space_id

    def get(self, kind, key):
        row = self.execute('SELECT body FROM memory_workflow_state WHERE space_id=:space AND kind=:kind AND id=:id',
                           {'space': self.space_id, 'kind': kind, 'id': key}).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, kind):
        rows = self.execute('SELECT body FROM memory_workflow_state WHERE space_id=:space AND kind=:kind ORDER BY id',
                            {'space': self.space_id, 'kind': kind}).fetchall()
        return [json.loads(row[0]) for row in rows]

    def put(self, kind, key, value):
        self.execute('INSERT INTO memory_workflow_state(space_id,kind,id,body) VALUES(:space,:kind,:id,:body) '
                     'ON CONFLICT(space_id,kind,id) DO UPDATE SET body=excluded.body',
                     {'space': self.space_id, 'kind': kind, 'id': key, 'body': json.dumps(value, ensure_ascii=False)})


@contextmanager
def transaction(space_id):
    if settings.STORAGE_BACKEND == 'postgres':
        from infrastructure.database import session_scope
        with session_scope() as session:
            lock = int.from_bytes(hashlib.sha256(('memory-workflow:' + space_id).encode()).digest()[:8], 'big') % (2**63)
            session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock})
            yield State(lambda sql, params: session.execute(text(sql), params), space_id)
    else:
        from memory import repository
        with repository._connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS memory_workflow_state '
                         '(space_id TEXT NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(space_id,kind,id))')
            conn.execute('BEGIN IMMEDIATE')
            yield State(conn.execute, space_id)


def get(space_id, kind, key):
    with transaction(space_id) as state:
        return state.get(kind, key)


def list_items(space_id, kind):
    with transaction(space_id) as state:
        return state.list(kind)


def put(space_id, kind, key, value):
    with transaction(space_id) as state:
        state.put(kind, key, value)
