"""Boundary tests: context propagation, errors, credentials and page-only delivery."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
import pytest
from core import lifecycle as trace


def test_span_parent_concurrency_and_error(monkeypatch):
    rows=[]
    monkeypatch.setattr(trace,'_save',lambda r:rows.append(dict(r)))
    @trace.traced('child')
    def child(value):
        assert trace.current()['trace_id']=='request-a'
        return {'value':value}
    with trace.resume({'trace_id':'request-a'}), trace.span('parent'):
        with ThreadPoolExecutor(2) as pool:
            assert trace.submit_with_context(pool,child,2).result()=={'value':2}
        with pytest.raises(ValueError),trace.span('failed'):
            raise ValueError('provider unavailable')
    assert not trace.current()
    completed=[r for r in rows if r['finished_at']]
    parent=next(r for r in completed if r['name']=='parent')
    assert all(r['parent_id']==parent['id'] for r in completed if r['name']!='parent')
    assert next(r for r in completed if r['name']=='failed')['status']=='error'
    assert all(r['duration_ms']>=0 for r in completed)


def test_no_context_no_persistence(monkeypatch):
    save=Mock();monkeypatch.setattr(trace,'_save',save)
    with trace.span('untraced'):pass
    trace.event('untraced')
    save.assert_not_called()


def test_secret_redaction():
    value=trace.safe({'api_key':'secret-value','nested':{'Authorization':'Bearer credential'},'content':'ordinary text'})
    assert 'secret-value' not in str(value)
    assert 'credential' not in str(value)
    assert value['content']=='ordinary text'


def test_console_auth_and_tenant(monkeypatch):
    from fastapi.testclient import TestClient
    from apps.api import app
    from repositories.postgres import lifecycle as store
    monkeypatch.setenv('SUIXINJI_CONSOLE_ENABLED','1')
    monkeypatch.setenv('SUIXINJI_CONSOLE_TOKEN','test-secret')
    monkeypatch.setenv('SUIXINJI_CONSOLE_TENANT','tenant-a')
    called=[]
    monkeypatch.setattr(store,'list_runs',lambda tenant:called.append(tenant) or [])
    monkeypatch.setattr(store,'detail',lambda ident,tenant:None)
    client=TestClient(app)
    assert client.get('/api/console/traces').status_code==401
    headers={'Authorization':'Bearer test-secret'}
    assert client.get('/api/console/traces',headers=headers).json()==[]
    assert called==['tenant-a']
    assert client.get('/api/console/traces/not-owned',headers=headers).status_code==404
    assert client.get('/console').status_code==200
    assert client.post('/api/console/messages',headers=headers,json={'text':'','mode':'ask'}).status_code==422


def test_console_answer_never_enqueues_feishu(monkeypatch):
    from apps import handlers
    from repositories.postgres import lifecycle as store
    enqueue=Mock();save=Mock()
    monkeypatch.setattr(handlers,'enqueue_task',enqueue)
    monkeypatch.setattr(store,'update_run',save)
    monkeypatch.setattr(trace,'_save',lambda r:None)
    with trace.resume({'trace_id':'console-request'}):
        handlers._enqueue_delivery({},text='answer',payload={'reply_channel':'console'})
    enqueue.assert_not_called()
    save.assert_called_once_with('console-request',answer='answer')


def test_console_messages_keep_preference_notification_route(monkeypatch):
    from types import SimpleNamespace
    from apps import console, receiver, api
    from core import settings
    from repositories.postgres import lifecycle as store
    commands = []
    monkeypatch.setattr(settings, 'STORAGE_BACKEND', 'postgres')
    monkeypatch.setattr(settings, 'TASK_QUEUE_BACKEND', 'redis_streams')
    monkeypatch.setattr(settings, 'PREFERENCE_CARDS_ENABLED', True)
    monkeypatch.setattr(store, 'create_run', lambda **kwargs: None)
    monkeypatch.setattr(store, 'update_run', lambda *a, **kwargs: None)
    monkeypatch.setattr(trace, '_save', lambda row: None)
    monkeypatch.setattr(api, '_check_rate_limit', lambda *args: None)
    monkeypatch.setattr(receiver, 'receive', lambda command: commands.append(command) or SimpleNamespace(task_id='task'))
    result = console.send(console.Message(text='他喜欢咖啡'), tenant='default')
    assert commands[0].chat_id == 'console:' + result['trace_id']
    assert commands[0].sender == {'user_id': 'console-user'}


@pytest.mark.parametrize('message,task,mode',[
    ('hello','ingest','ingest'),('/ask 饮品偏好','query','ask'),('/summary 今天','summary','summary'),
    ('/type 生活','query','/type'),('/tag 饮食','query','/tag'),('/filter type=生活','query','/filter'),
    ('/memory list','query','/memory'),('/trace latest','query','/trace'),('/status','query','/status'),
    ('/feedback 内容','query','/feedback'),('/summary_auto status','query','/summary_auto'),('/unknown','query','/unknown')])
def test_all_message_routes(message,task,mode):
    from apps.console_commands import route_message
    route=route_message(message)
    assert route['task_type']==task
    assert route['mode']==mode


def test_command_menu_covers_memory_actions():
    from apps.console_commands import COMMANDS
    assert len([c for c in COMMANDS if c['command'].startswith('/memory ')])==17


def test_cross_space_memory_rejected(monkeypatch):
    from apps.console_commands import execute_command
    from memory import repository
    from types import SimpleNamespace
    monkeypatch.setattr(repository,'get_memory',lambda ident:SimpleNamespace(space_id='other'))
    with pytest.raises(ValueError,match='不属于'):
        execute_command('mine','/memory forget memory-other')


def test_traced_database_writes_use_separate_pool(monkeypatch):
    from repositories.postgres import lifecycle as store
    engine=Mock()
    engine.begin.return_value.__enter__=Mock(return_value=Mock())
    engine.begin.return_value.__exit__=Mock(return_value=False)
    calls=[]
    monkeypatch.setattr(store,'get_engine',lambda role:calls.append(role) or engine)
    store.save_span({'id':'id','trace_id':'trace'})
    assert calls==['lifecycle']


def test_mutation_span_keeps_before_and_after(monkeypatch):
    from dataclasses import dataclass
    @dataclass
    class Row:
        content:str
    rows=[]
    monkeypatch.setattr(trace,'_save',lambda r:rows.append(dict(r)))
    @trace.traced('update',output_arg='row')
    def update(row):
        row.content='after'
    with trace.resume({'trace_id':'mutation'}):
        update(Row('before'))
    assert rows[-1]['input_json']['row']['content']=='before'
    assert rows[-1]['output_json']['content']=='after'


def test_scheduler_console_destination_cannot_reach_feishu(monkeypatch):
    from runtime.stream_dispatcher import StreamTaskDispatcher
    from apps import console_commands
    expected=object()
    call=Mock(return_value=expected)
    monkeypatch.setattr(console_commands,'enqueue_scheduled_summary',call)
    result=StreamTaskDispatcher().submit_summary('space','today','console:default',delivery_key='auto:2026-10-18',delivery_type='auto_summary')
    assert result is expected
    assert call.call_args.args[1]['sent_date']=='2026-10-18'


def test_trace_storage_failure_does_not_fail_business(monkeypatch):
    from repositories.postgres import lifecycle as store
    monkeypatch.setattr(store,'save_span',Mock(side_effect=RuntimeError('db down')))
    with trace.resume({'trace_id':'test'}),trace.span('business') as observation:
        observation['output']='business completed'
    assert not trace.current()


def test_scheduled_summary_has_no_empty_inbox_dependency(monkeypatch):
    from apps import handlers
    monkeypatch.setattr(handlers,'FAKE_EXTERNALS',True)
    monkeypatch.setattr(handlers,'_enqueue_delivery',lambda *a,**k:None)
    result=handlers.handle_summary({'id':'scheduled','space_id':'space','payload_json':{'range_key':'today'}})
    assert result.release_inbox_id is None


def test_queue_wait_uses_publisher_clock(monkeypatch):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from repositories.postgres import tasks
    rows = []
    monkeypatch.setattr(trace, '_save', lambda row: rows.append(dict(row)))
    monkeypatch.setattr(tasks, 'get_task', lambda _: {'status': 'completed'})
    message = SimpleNamespace(message_id='1-0', stream='query', fields={
        'trace_id': 'clock-test', 'task_id': 'test',
        'published_at': datetime.now(timezone.utc).isoformat(),
    })
    trace.trace_message(lambda self, msg: None)(SimpleNamespace(task_type='query'), message)
    wait = next(row for row in rows if row['name'] == 'Redis · 队列等待')
    assert 0 <= wait['duration_ms'] < 1000


def lifecycle_retry_fixture():
    from datetime import datetime, timezone, timedelta
    start = datetime(2026, 10, 21, tzinfo=timezone.utc)
    end = start + timedelta(seconds=150)
    run = {'created_at': start, 'error': None, 'answer': None}
    nodes = [dict(id='old', parent_id=None, kind='worker', input_json={'task_id':'t'},
                  started_at=start, finished_at=None, status='running', metadata_json={}),
             dict(id='child', parent_id='old', kind='llm', started_at=start,
                  finished_at=None, status='running', metadata_json={}),
             dict(id='new', parent_id=None, kind='worker', input_json={'task_id':'t'},
                  started_at=end, finished_at=None, status='running', metadata_json={})]
    tasks = [dict(id='t', status='completed', completed_at=end)]
    attempts = [dict(task_id='t', attempt_no=1, status='lease_expired',
                     started_at=start, finished_at=end),
                dict(task_id='t', attempt_no=2, status='completed',
                     started_at=end, finished_at=end)]
    return run, nodes, tasks, attempts


def test_completed_retry_reconciles_old_spans_without_rewriting_history():
    from repositories.postgres.lifecycle import project_detail
    run, nodes, tasks, attempts = lifecycle_retry_fixture()
    result = project_detail(run, nodes, tasks, attempts)
    assert result['status'] == 'completed'
    assert result['duration_ms'] == 150000
    old, child, new = result['spans']
    assert old['status'] == child['status'] == 'interrupted'
    assert old['metadata_json']['attempt_status'] == 'lease_expired'
    assert old['finished_at'] is None  # Do not invent the interruption time.
    assert nodes[0]['status'] == 'running' and nodes[0]['metadata_json'] == {}


@pytest.mark.parametrize('state,expected', [('running','running'), ('retry','retry'),
                                          ('dead_letter','error'), ('cancelled','error')])
def test_old_expired_attempt_does_not_hide_current_task_state(state, expected):
    from repositories.postgres.lifecycle import project_detail
    run, nodes, tasks, attempts = lifecycle_retry_fixture()
    tasks[0].update(status=state, completed_at=None)
    attempts[1].update(status='running', finished_at=None)
    result = project_detail(run, nodes, tasks, attempts)
    assert result['status'] == expected
    assert result['spans'][0]['status'] == 'interrupted'
    assert result['spans'][2]['status'] == 'running'


def test_unowned_open_span_is_not_falsely_marked_interrupted():
    from repositories.postgres.lifecycle import project_detail
    run, nodes, tasks, attempts = lifecycle_retry_fixture()
    result = project_detail(run, nodes, [], [])
    assert result['status'] == 'running'
    assert all(n['status'] == 'running' for n in result['spans'])
    # Completed business tasks remain authoritative even with incomplete telemetry.
    assert project_detail(run, nodes, tasks, [])['status'] == 'completed'
