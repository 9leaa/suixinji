from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from apps.api import app
from core import answer_grounding as grounding
from repositories.postgres import user_profile


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('SUIXINJI_CONSOLE_ENABLED', '1')
    monkeypatch.setenv('SUIXINJI_CONSOLE_TOKEN', 'test-secret')
    monkeypatch.setenv('SUIXINJI_CONSOLE_TENANT', 'tenant-a')
    return TestClient(app, headers={'Authorization': 'Bearer test-secret'})


def test_alias_endpoints_scope_and_persistence_contract(client, monkeypatch):
    save = Mock()
    read = Mock(return_value='小舟')
    monkeypatch.setattr(user_profile, 'save_alias', save)
    monkeypatch.setattr(user_profile, 'get_alias', read)
    response = client.put('/api/console/profile', json={'user_id': 'u1', 'alias': ' 小舟 '})
    assert response.json() == {'user_id': 'u1', 'alias': '小舟'}
    save.assert_called_once_with('u1', '小舟', tenant_id='tenant-a')
    assert client.get('/api/console/profile?user_id=u1').json()['alias'] == '小舟'
    read.assert_called_once_with('u1', tenant_id='tenant-a')
    assert client.put('/api/console/profile', headers={'Authorization': 'bad'},
                      json={'user_id': 'u1', 'alias': '小舟'}).status_code == 401


@pytest.mark.parametrize('alias', ['', '   ', 'a\nb', '名' * 41])
def test_alias_invalid_input_is_not_saved(client, monkeypatch, alias):
    save = Mock()
    monkeypatch.setattr(user_profile, 'save_alias', save)
    assert client.put('/api/console/profile', json={'user_id': 'u1', 'alias': alias}).status_code == 422
    save.assert_not_called()


def test_alias_tenant_collision_is_rejected(client, monkeypatch):
    monkeypatch.setattr(user_profile, 'save_alias', Mock(side_effect=ValueError('其他租户')))
    assert client.put('/api/console/profile', json={'user_id': 'u1', 'alias': '小舟'}).status_code == 409


def test_alias_overrides_name_and_refreshes_next_run(monkeypatch):
    from agent.hooks.context import AgentRunContext
    from infrastructure import database
    profile = {'alias': '小舟', 'name': '账号姓名'}

    @contextmanager
    def session():
        db = Mock()
        db.execute.return_value.scalar_one_or_none.return_value = profile
        yield db

    monkeypatch.setattr(database, 'session_scope', session)
    monkeypatch.setattr(grounding.settings, 'STORAGE_BACKEND', 'postgres')
    first = AgentRunContext.create(space_id='s', run_type='query', user_id='u1')
    assert grounding.identity_for_run(first) == {'id': 'u1', 'name': '小舟', 'name_source': 'user_alias'}
    profile['alias'] = '小山'
    second = AgentRunContext.create(space_id='s', run_type='query', user_id='u1')
    assert grounding.identity_for_run(second)['name'] == '小山'
    assert grounding.identity_for_run(first)['name'] == '小舟'
