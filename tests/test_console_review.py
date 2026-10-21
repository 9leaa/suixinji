from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from apps import console_library as library


def test_version_comparison_preserves_recorded_content_and_missing_before():
    versions = [{'version': 3, 'content': 'new', 'reason': 'correction'}, {'version': 2, 'content': 'old'}]
    pairs = library.version_pairs(versions)
    assert pairs[0]['before'] is None
    assert pairs[1]['before']['content'] == 'old'
    assert pairs[1]['after']['content'] == 'new'
    assert pairs[1]['reason'] == 'correction'
    assert versions[0]['version'] == 3


def test_library_auth_scope_filter_and_pagination(monkeypatch):
    monkeypatch.setenv('SUIXINJI_CONSOLE_ENABLED', '1')
    monkeypatch.setenv('SUIXINJI_CONSOLE_TOKEN', 'test-only')
    monkeypatch.setenv('SUIXINJI_CONSOLE_TENANT', 'tenant')
    seen = []
    def check(space, tenant):
        if space != 'allowed' or tenant != 'tenant':
            raise HTTPException(404, 'space not found')
    monkeypatch.setattr(library, 'check_space', check)
    monkeypatch.setattr(library, 'library_rows', lambda space: seen.append(space) or [
        {'id': str(i), 'memory_type': 'task' if i % 2 else 'preference', 'content': 'coffee', 'updated_at': str(i)}
        for i in range(6)])
    app = FastAPI()
    app.include_router(library.router)
    client = TestClient(app)
    headers = {'Authorization': 'Bearer test-only'}
    assert client.get('/api/console/memories?space=allowed').status_code == 401
    assert client.get('/api/console/memories?space=denied', headers=headers).status_code == 404
    assert not seen
    result = client.get('/api/console/memories?space=allowed&kind=task&limit=1&offset=1', headers=headers).json()
    assert result['total'] == 3 and result['items'][0]['id'] == '3'
    assert result['counts']['preference'] == 3
    assert client.get('/api/console/memories?space=allowed&offset=-1', headers=headers).status_code == 422


def test_history_filter_validation_and_tenant(monkeypatch):
    from apps import console
    monkeypatch.setenv('SUIXINJI_CONSOLE_ENABLED', '1')
    monkeypatch.setenv('SUIXINJI_CONSOLE_TOKEN', 'test-only')
    monkeypatch.setenv('SUIXINJI_CONSOLE_TENANT', 'tenant')
    called = []
    monkeypatch.setattr(console.store, 'search_runs', lambda tenant, **kw: called.append((tenant, kw)) or {'items': []})
    app = FastAPI()
    app.include_router(console.router)
    client = TestClient(app)
    headers = {'Authorization': 'Bearer test-only'}
    assert client.get('/api/console/history?since=bad', headers=headers).status_code == 422
    assert client.get('/api/console/history?limit=500', headers=headers).status_code == 422
    assert not called
    assert client.get('/api/console/history?space=s&status=retry&offset=30', headers=headers).status_code == 200
    assert called[0][0] == 'tenant' and called[0][1]['offset'] == 30
    assert client.get('/api/console/history?since=2026-01-01&until=2026-01-02T00:00:00Z', headers=headers).status_code == 200


def test_library_versions_hide_forgotten_sources(monkeypatch):
    from memory import fact_memory as fm
    from apps.fact_observations import protect_history
    fm.store.put('s', 'fm_tombstone', 't', {'note_id': 'n', 'evidence_span': 'secret'})
    protected = protect_history('s', [{'id': 'm', 'version': 1, 'content': 'secret', 'source_note_id': 'n'}])
    result = library.version_pairs(protected)
    assert 'secret' not in str(result)
    assert result[0]['after']['withheld']
