"""Public console authentication, session revocation, and ownership boundaries."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from apps.api import app
from apps import console_auth as auth
from repositories.postgres import console_auth as store

HEADERS = {'X-Suixinji-Request': 'console'}
PASSWORD = 'test-password-long-enough'


@pytest.fixture
def accounts(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    store.metadata.create_all(engine)
    monkeypatch.setattr(store, 'get_engine', lambda: engine)
    monkeypatch.setattr(store, 'provision_space', lambda *args: None)
    monkeypatch.setenv('SUIXINJI_CONSOLE_ENABLED', '1')
    monkeypatch.setenv('SUIXINJI_CONSOLE_AUTH_MODE', 'accounts')
    monkeypatch.setenv('SUIXINJI_CONSOLE_REGISTRATION_ENABLED', 'true')
    monkeypatch.setenv('SUIXINJI_CONSOLE_COOKIE_SECURE', 'false')
    monkeypatch.setenv('SUIXINJI_CONSOLE_TOKEN', 'old-admin-token')
    from infrastructure import redis_rate_limit
    limiter = redis_rate_limit.LocalRateLimiter()
    monkeypatch.setattr(redis_rate_limit, 'RedisRateLimiter', lambda: limiter)
    yield engine
    engine.dispose()


def register(name):
    client = TestClient(app, headers=HEADERS)
    response = client.post('/api/console/auth/register', json={
        'username': name, 'password': PASSWORD, 'alias': name, 'is_admin': True, 'tenant_id': 'default'})
    assert response.status_code == 201, response.text
    return client, response.json()


def test_registration_hashes_password_and_ignores_privilege_fields(accounts):
    client, account = register('Alice')
    assert account['username'] == 'alice'
    assert account['is_admin'] is False
    stored = store.find_account('alice')
    assert stored['tenant_id'] != 'default'
    assert stored['password_hash'] != PASSWORD
    assert store.verify_password(PASSWORD, stored['password_hash'])
    assert 'password_hash' not in account
    cookie = client.cookies.get(auth.COOKIE)
    with accounts.connect() as conn:
        assert conn.scalar(select(store.sessions.c.token_hash)) == store.token_hash(cookie)
    duplicate = client.post('/api/console/auth/register', json={'username': 'ALICE', 'password': PASSWORD, 'alias': '另一人'})
    assert duplicate.status_code == 409


def test_login_logout_expiry_and_old_token_cannot_bypass(accounts):
    client, account = register('alice')
    cookie = client.cookies.get(auth.COOKIE)
    assert client.get('/api/console/auth/me').json() == account
    assert client.post('/api/console/auth/logout', json={}).status_code == 200
    assert store.resolve_session(cookie) is None
    assert client.get('/api/console/traces', headers={'Authorization': 'Bearer old-admin-token'}).status_code == 401
    assert client.post('/api/console/auth/login', json={'username': 'alice', 'password': 'wrong-password'}).status_code == 401
    response = client.post('/api/console/auth/login', json={'username': 'alice', 'password': PASSWORD})
    assert response.status_code == 200
    assert 'HttpOnly' in response.headers['set-cookie'] and 'SameSite=strict' in response.headers['set-cookie']
    with accounts.begin() as conn:
        conn.execute(store.sessions.update().values(expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
    assert client.get('/api/console/auth/me').status_code == 401


def test_csrf_and_closed_registration(accounts, monkeypatch):
    client = TestClient(app)
    body = {'username': 'alice', 'password': PASSWORD, 'alias': 'Alice'}
    assert client.post('/api/console/auth/register', json=body).status_code == 403
    monkeypatch.setenv('SUIXINJI_CONSOLE_REGISTRATION_ENABLED', 'false')
    assert client.post('/api/console/auth/register', headers=HEADERS, json=body).status_code == 403
    monkeypatch.setenv('SUIXINJI_CONSOLE_REGISTRATION_ENABLED', 'true')
    logged, _ = register('alice')
    assert logged.post('/api/console/auth/logout', headers={'X-Suixinji-Request': ''}, json={}).status_code == 403
    assert logged.get('/api/console/auth/me').status_code == 200


def test_password_change_revokes_all_sessions(accounts):
    first, _ = register('alice')
    second = TestClient(app, headers=HEADERS)
    second.post('/api/console/auth/login', json={'username': 'alice', 'password': PASSWORD})
    assert first.post('/api/console/auth/password', json={'current_password': 'wrong', 'new_password': 'new-password-long-enough'}).status_code == 401
    assert first.post('/api/console/auth/password', json={'current_password': PASSWORD, 'new_password': 'new-password-long-enough'}).status_code == 200
    assert first.get('/api/console/auth/me').status_code == 401
    assert second.get('/api/console/auth/me').status_code == 401


def test_identity_cannot_be_chosen_by_browser(accounts, monkeypatch):
    from apps import receiver, api
    from repositories.postgres import lifecycle, user_profile
    from core import settings, lifecycle as tracing
    client, alice = register('alice')
    _, bob = register('bob')
    monkeypatch.setattr(settings, 'STORAGE_BACKEND', 'postgres')
    monkeypatch.setattr(settings, 'TASK_QUEUE_BACKEND', 'redis_streams')
    create_run = Mock()
    monkeypatch.setattr(lifecycle, 'create_run', create_run)
    monkeypatch.setattr(lifecycle, 'update_run', Mock())
    monkeypatch.setattr(tracing, '_save', Mock())
    monkeypatch.setattr(api, '_check_rate_limit', Mock())
    receive = Mock(return_value=SimpleNamespace(task_id='task-a'))
    monkeypatch.setattr(receiver, 'receive', receive)
    response = client.post('/api/console/messages', json={'text': '我喜欢咖啡', 'space_id': bob['space_id'], 'user_id': bob['user_id']})
    assert response.status_code == 202
    command = receive.call_args.args[0]
    assert command.space_id == alice['space_id']
    assert command.sender['user_id'] == alice['user_id']
    assert command.tenant_id == store.find_account('alice')['tenant_id']
    assert create_run.call_args.kwargs['space_id'] == alice['space_id']
    read, write = Mock(return_value='Alice'), Mock()
    monkeypatch.setattr(user_profile, 'get_alias', read)
    monkeypatch.setattr(user_profile, 'save_alias', write)
    assert client.get('/api/console/profile', params={'user_id': bob['user_id']}).json()['user_id'] == alice['user_id']
    client.put('/api/console/profile', json={'user_id': bob['user_id'], 'alias': 'New name'})
    assert write.call_args.args[0] == alice['user_id']


def test_history_scope_and_diagnostics_permissions(accounts, monkeypatch):
    from repositories.postgres import lifecycle
    client, _ = register('alice')
    other, _ = register('bob')
    tenant = store.find_account('alice')['tenant_id']
    private = {'id': 'trace-a', 'space_id': 'a', 'text': 'private note', 'answer': 'answer',
               'spans': [{'input_json': 'system prompt'}], 'tasks': [{'last_error': 'internal detail'}],
               'config_json': {'model': 'internal'}, 'error': 'internal stack'}
    monkeypatch.setattr(lifecycle, 'detail', lambda ident, scope: private if ident == 'trace-a' and scope == tenant else None)
    monkeypatch.setattr(lifecycle, 'list_runs', lambda scope: [private] if scope == tenant else [])
    assert other.get('/api/console/traces/trace-a').status_code == 404
    assert other.get('/api/console/traces').json() == []
    visible = client.get('/api/console/traces/trace-a').json()
    assert visible['text'] == 'private note' and visible['spans'] == [] and visible['tasks'] == []
    assert 'internal' not in str(visible)
    assert client.get('/api/fact-memory/a/observations').status_code == 403
    assert client.post('/api/console/messages', json={'text': '/status'}).status_code == 403
    assert client.post('/api/console/messages', json={'text': '/trace latest'}).status_code == 403
    assert client.get('/api/console/config').json()['diagnostics'] is False
    assert '/status' not in [r['command'] for r in client.get('/api/console/commands').json()]


def test_auth_rate_limit(accounts):
    client = TestClient(app, headers=HEADERS)
    for _ in range(10):
        assert client.post('/api/console/auth/login', json={'username': 'unknown', 'password': PASSWORD}).status_code == 401
    assert client.post('/api/console/auth/login', json={'username': 'unknown', 'password': PASSWORD}).status_code == 429


def test_secure_cookie_default(accounts, monkeypatch):
    monkeypatch.delenv('SUIXINJI_CONSOLE_COOKIE_SECURE')
    client = TestClient(app, base_url='https://testserver', headers=HEADERS)
    response = client.post('/api/console/auth/register', json={'username': 'alice', 'password': PASSWORD, 'alias': 'Alice'})
    assert 'Secure' in response.headers['set-cookie']
    assert client.get('/api/console/auth/me').status_code == 200


@pytest.mark.parametrize('operation', ['show', 'SHOW', 'Correct', 'PURGE', 'APPROVE'])
def test_memory_commands_check_ownership_case_insensitively(monkeypatch, operation):
    from apps.console_commands import execute_command
    from memory import repository
    monkeypatch.setattr(repository, 'get_memory', lambda ident: SimpleNamespace(space_id='other-space'))
    with pytest.raises(ValueError, match='当前空间'):
        execute_command('my-space', '/memory ' + operation + ' other-memory new content')
