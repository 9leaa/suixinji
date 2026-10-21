"""Simplified task cards: real isolated storage with deterministic model outputs."""
from unittest.mock import Mock
from dataclasses import replace
import pytest
from pydantic import ValidationError
from core import settings
from memory import extractor, service, repository as repo, task_cards as cards
from memory.task_schema import TaskInput


@pytest.fixture(autouse=True)
def task_mode(monkeypatch):
    monkeypatch.setattr(settings, 'TASK_CARDS_ENABLED', True)
    for module in (extractor, service):
        monkeypatch.setattr(module, 'MEMORY_EXTRACTOR_MODE', 'llm')
    monkeypatch.setattr(extractor, '_LLM_CONNECTION_CIRCUIT_OPEN_UNTIL', 0)
    from memory import task_retrieval
    monkeypatch.setattr(task_retrieval, 'vector_rank', lambda *a: [])


def row(text='提交周报', **kw):
    return dict(memory_type='task', subject='用户', task='提交周报', task_status='todo', description=text, evidence_span=text) | kw


def write(monkeypatch, text='提交周报', *, key='n1', data=None, **kw):
    monkeypatch.setattr(extractor, 'complete_json', Mock(return_value={'candidates':[data or row(text)]}))
    result=service.process_note_memory(dict(id=key, space_id='s', text=text, ts='2026-10-21T09:00:00+08:00', **kw))
    assert result['extraction_status']=='completed', result
    return result['results'][0]


def update_plan(monkeypatch, card, description):
    monkeypatch.setattr(cards, 'complete_json', Mock(return_value=dict(action='update',target_id=card['id'], description=description,
        subject_relation='same',instance_relation='same',reason='同一任务的新消息')))


def test_description_update_keeps_identity_sources_and_version_log(monkeypatch):
    first=write(monkeypatch,'周五交周报，但网络断了')['task_card']
    update_plan(monkeypatch,first,'周五交周报，网络已恢复，尚未提交。')
    second=write(monkeypatch,'网络恢复了，周报还没提交',key='n2')['task_card']
    assert second['id']==first['id'] and second['task_status']=='todo'
    assert second['description']=='周五交周报，网络已恢复，尚未提交。'
    assert 'details' not in second and 'closure_reason' not in second
    assert len(second['sources'])==2
    record=repo.get_memory(second['id'])
    assert record.current_version==2
    assert any('网络断了' in v.content for v in record.versions)
    hits=service.memory_search('s','周报',memory_type='task',min_score=0)
    assert hits[0]['description']==second['description']
    from agent.ask_executor import _as_evidence
    assert _as_evidence(hits[0],tool='memory_search',role='supporting').task_card['description']==second['description']


@pytest.mark.parametrize('text',['周报已完成','周报取消，不再提交','放弃提交周报'])
def test_done_includes_completed_cancelled_abandoned_and_is_recalled(monkeypatch,text):
    first=write(monkeypatch)['task_card']
    update_plan(monkeypatch,first,text)
    done=write(monkeypatch,text,key='n2',data=row(text,task_status='done'))['task_card']
    assert done['task_status']=='done' and done['description']==text
    assert done['id'] in {r.id for r in cards.retrieve('s',TaskInput(**row()),'owner')}
    update_plan(monkeypatch,done,'重新开始提交周报')
    reopened=write(monkeypatch,'重新开始提交周报',key='n3',data=row('重新开始提交周报',intent='reopen'))['task_card']
    assert reopened['id']==done['id'] and reopened['task_status']=='todo'


def test_done_cannot_silently_reopen_and_new_round_gets_new_card(monkeypatch):
    old=write(monkeypatch,'周报完成',data=row('周报完成',task_status='done'))['task_card']
    update_plan(monkeypatch,old,'周报还有补充说明')
    pending=write(monkeypatch,'周报还有补充说明',key='n2')
    assert pending['action']=='pending_review'
    fresh=write(monkeypatch,'再写下一份周报',key='n3',data=row('再写下一份周报',intent='new'))['task_card']
    assert fresh['id']!=old['id'] and fresh['task_status']=='todo'


def test_legacy_projection_preserves_cancellation_without_writing(monkeypatch):
    saved=write(monkeypatch)['task_card']
    record=repo.get_memory(saved['id'])
    old={'subject':'用户','task':'提交周报','instance':None,'task_status':'done','closure_reason':'cancelled',
         'sources':saved['sources'],'details':[{'kind':'blocker','description':'网络断了','status':'history'},
         {'kind':'requirement','description':'周五交','status':'active','condition':''}]}
    legacy=replace(record,memory_key_version='task-card-v1',scope={'task_card':old})
    projected=cards.project(legacy)
    assert projected['description']=='周五交；已取消'
    assert 'details' not in projected and legacy.scope['task_card']==old


def test_legacy_fields_and_third_status_are_not_valid_new_contract():
    for extra in [{'details':[]},{'closure_reason':'completed'},{'task_status':'blocked'}]:
        with pytest.raises(ValidationError):
            TaskInput(**(row()|extra))


def test_older_message_cannot_overwrite_current_description(monkeypatch):
    saved=write(monkeypatch)['task_card']
    record=repo.get_memory(saved['id'])
    value=TaskInput(**row())
    with pytest.raises(ValueError,match='早于'):
        cards.merge(value,record,cards.TaskPlan(action='update',target_id=record.id,description='旧内容'),
                    {'id':'old','note_id':'old','message_time':'2026-10-20T09:00:00+08:00'})


def test_replay_keeps_latest_version_and_timeline_is_available(monkeypatch):
    first=write(monkeypatch)['task_card']
    update_plan(monkeypatch,first,'周五交周报')
    latest=write(monkeypatch,'周五交周报',key='n2')['task_card']
    replay=cards.apply_task(cards.candidate_from_row('n1','提交周报',row()),
        {'id':'n1','space_id':'s','text':'提交周报','ts':'2026-10-21T09:00:00+08:00'})
    assert replay['idempotent'] and replay['task_card']['description']==latest['description']
    timeline=repo.get_memory_timeline('s',memory_id=first['id'])
    assert timeline[0]['description']==latest['description'] and len(timeline[0]['versions'])==2
    assert repo.get_memory(first['id']).current_version==2


def test_owner_isolation(monkeypatch):
    first=write(monkeypatch,user_id='one')['task_card']
    assert first['id'] not in {r.id for r in cards.records('s','two')}


def test_version_conflict_recomputes_full_description(monkeypatch):
    first=write(monkeypatch)['task_card']
    update_plan(monkeypatch,first,'周五交周报')
    real=repo.apply_memory_decision
    calls=[]
    def conflict_once(*args,**kw):
        calls.append(1)
        if len(calls)==1:
            raise cards.TaskVersionConflict('concurrent update')
        return real(*args,**kw)
    monkeypatch.setattr(repo,'apply_memory_decision',conflict_once)
    updated=write(monkeypatch,'周五交周报',key='n2')
    assert updated['task_card']['id']==first['id']
    assert cards.complete_json.call_count==2
