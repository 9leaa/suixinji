import pytest
from core import settings
from memory import fact_memory as fm


def test_context_snapshot_keeps_recall_separate_from_recent_and_survives_edit(monkeypatch):
    from memory.fact_extraction import context
    monkeypatch.setattr(settings, 'FACT_MEMORY_ENABLED', True)
    f = fm.ingest_fact('s', note_id='old', original_text='露台的花盆是蓝色', content='露台的花盆是蓝色', memory_type='semantic', enqueue=False)
    monkeypatch.setattr(fm, 'search_memory', lambda *a, **k: [])
    result = context('s', 'new', '这个花盆移到室内了')
    snapshot = fm.get('s', 'extraction_context', 'new')
    assert snapshot['retrieved'] == []
    assert snapshot['model_context'] == result and result[0]['id'] == f['id']
    f['content'] = 'new value'
    fm.store.put('s', 'fm_fact', f['id'], f)
    assert snapshot['model_context'][0]['content'] == '露台的花盆是蓝色'


def test_diagnostic_history_hides_forgotten_sources():
    from apps.fact_observations import protect_history
    fm.store.put('s', 'fm_tombstone', 't', {'note_id':'n', 'evidence_span':'忘掉的片段'})
    rows = [{'id':'run1', 'trace':[{'source_fact_ids':['f'], 'text':'忘掉的片段'}]},
            {'id':'run2', 'context':[{'note_id':'n', 'content':'改写后的片段'}]},
            {'id':'run3', 'content':'其他内容'}]
    out = protect_history('s', rows)
    assert out[0].get('withheld') and out[1].get('withheld')
    assert '忘掉的片段' not in str(out) and '改写后的片段' not in str(out)
    assert out[2]['content'] == '其他内容'
    assert len(protect_history('other', rows)) == 3


def test_observation_api_checks_authorization_and_space_before_read(monkeypatch):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from apps import fact_console, fact_observations
    app=FastAPI();app.include_router(fact_console.router)
    monkeypatch.setenv('SUIXINJI_CONSOLE_ENABLED','1')
    monkeypatch.setenv('SUIXINJI_CONSOLE_TOKEN','test-only')
    calls=[]
    def check(space, tenant):
        if space != 'allowed': raise HTTPException(404, 'space not found')
    monkeypatch.setattr(fact_console, 'check_space', check)
    monkeypatch.setattr(fact_observations, 'read_observations', lambda space: calls.append(space) or {'space_id':space})
    client=TestClient(app);headers={'Authorization':'Bearer test-only'}
    assert client.get('/api/fact-memory/allowed/observations').status_code == 401
    assert client.get('/api/fact-memory/denied/observations',headers=headers).status_code == 404
    assert not calls
    assert client.get('/api/fact-memory/allowed/observations',headers=headers).json()['space_id'] == 'allowed'


def test_query_tool_return_and_model_context_are_distinct_snapshots(monkeypatch):
    from agent import query_agent as qa
    monkeypatch.setattr(settings, 'FACT_MEMORY_ENABLED', True)
    monkeypatch.setattr(qa, '_execute_tool', lambda *a, **k: [{'id':'mem1','content':'正文', 'sources':[{'original_text':'原文片段'}]}])
    trace={'steps':[]}
    rows=qa._run_tool('s','memory_search',{'query':'口味','memory_type':'preference'},trace=trace)
    assert trace['steps'][0]['input_summary']['memory_type']=='preference'
    assert trace['steps'][0]['output_summary']['records']==rows
    monkeypatch.setattr(qa,'_complete_json_with_hooks',lambda *a, **k:{'final_answer':'答案'})
    qa._synthesize_answer('问题',[{'tool':'memory_search','result':rows}],trace=trace)
    assert trace['steps'][1]['step']=='model_context'
    assert trace['steps'][1]['input_summary']['observations'][0]['result']==rows
