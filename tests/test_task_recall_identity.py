"""Regression boundaries for parallel recall, identity decisions and account aliases."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import subject_identity as identity
from memory import task_retrieval as recall, task_cards, workflow_state
from memory.models import MemoryCandidate
from memory.task_schema import TaskInput


def value(**kw):
    return TaskInput(task='整理材料', task_status='todo', evidence_span='整理材料', **kw)


def pair(key, subject='用户', task='整理材料', instance=None, sources=None):
    return SimpleNamespace(id=key), {'subject':subject, 'task':task, 'instance':instance, 'sources':sources or []}


@pytest.mark.parametrize('kind', ['task', 'preference', 'semantic', 'episodic'])
@pytest.mark.parametrize('subject', [None, '', '我', '用户', '小舟'])
def test_account_identity_applies_to_every_type(monkeypatch, kind, subject):
    monkeypatch.setattr(identity, 'current_user_identity', lambda *a, **k: {'id':'u1','name':'小舟'})
    with identity.identity_context({'user_id':'u1'}):
        c = identity.normalize_candidate(MemoryCandidate(kind, '整理材料', .8, .9, subject=subject))
    assert c.subject == '用户'
    assert c.scope['subject_id'] == 'user:u1'
    assert identity.current_identity()['id'] is None


def test_third_person_context_and_same_name_are_not_self(monkeypatch):
    monkeypatch.setattr(identity, 'current_user_identity', lambda *a, **k: {'id':'u1','name':'小舟'})
    with identity.identity_context({'user_id':'u1'}):
        assert identity.canonical_subject('小舟', '同事小舟喜欢茶') == '小舟'
        assert identity.canonical_subject('李若宁', '已经做完了') == '李若宁'


def test_source_and_semantic_candidates_survive_subject_and_instance_mismatch(monkeypatch):
    # No shared title/person tokens: only the genuine vector channel can find b.
    monkeypatch.setattr(recall, 'embed_text', lambda text: [1.,0.] if '杂事' not in text else [0.,1.])
    cards = [pair('a','李若宁','清点设备','一次',[{'note_id':'old'}]), pair('b','别人','盘库','另一轮'), pair('c',task='杂事')]
    found, audit = recall.recall('s',value(subject='若宁',instance='刚才那次',antecedent_note_id='old'),cards)
    assert {r.id for r in found} == {'a','b','c'}
    assert found[0].id == 'a'
    assert audit['vector_status'] == 'ready'
    assert 'vector' in next(c for c in audit['candidates'] if c['id']=='b')['signals']
    assert len(workflow_state.list_items('s','task_identity_vector')) == 3
    mock = Mock(side_effect=AssertionError('unchanged card vectors should be cached'))
    # Query embeds once; each unchanged card reuses its vector.
    mock.side_effect = [[1.,0.]]
    monkeypatch.setattr(recall, 'embed_text', mock)
    recall.recall('s',value(),cards)
    assert mock.call_count == 1


def test_vector_failure_is_reported_without_losing_source(monkeypatch):
    monkeypatch.setattr(recall,'embed_text',Mock(side_effect=RuntimeError('unavailable')))
    found,audit=recall.recall('s',value(antecedent_note_id='old'),[pair('a',sources=[{'note_id':'old'}])])
    assert found[0].id=='a'
    assert audit['vector_status']=='unavailable:RuntimeError'


@pytest.mark.parametrize('left,right,conflict', [('一次','刚才那次',False),('本次','原来这次',False),('2026-10-21航班','2026-10-28的航班',True),('第43周','第44周',True),('2026-10-19至2026-10-25周报','2026-10-19至2026-10-25的项目周报',False)])
def test_instance_identity_uses_anchors_not_display_text(left,right,conflict):
    assert recall.instance_conflict(left,right) is conflict


def test_changed_names_require_explicit_identity_decision(monkeypatch):
    record = SimpleNamespace(id='a')
    card = {'subject':'李若宁','instance':'一次','task_status':'todo','details':[]}
    monkeypatch.setattr(task_cards,'project',lambda r:card)
    incoming=value(subject='若宁',instance='刚才那次')
    with pytest.raises(ValueError,match='主体'):
        task_cards.check_plan(incoming,task_cards.TaskPlan(action='update',target_id='a'),[record])
    plan=task_cards.TaskPlan(action='update',target_id='a',subject_relation='same',instance_relation='same',reason='前文明确说明李若宁简称若宁，指向同一次')
    assert task_cards.check_plan(incoming,plan,[record]) is record
    card['instance']='2026-10-21航班'
    with pytest.raises(ValueError,match='日期'):
        task_cards.check_plan(value(subject='若宁',instance='2026-10-28航班'),plan,[record])


def test_same_task_for_different_people_may_create_separate_cards(monkeypatch):
    record = SimpleNamespace(id='a')
    monkeypatch.setattr(task_cards,'project',lambda r: {'subject':'李若宁','task':'整理材料','instance':None})
    assert task_cards.check_plan(value(subject='陈明'),task_cards.TaskPlan(action='new'),[record]) is None


def test_negative_identity_decision_cannot_write_even_when_labels_match(monkeypatch):
    record = SimpleNamespace(id='a')
    monkeypatch.setattr(task_cards,'project',lambda r: {'subject':'用户','task':'整理材料','instance':None})
    with pytest.raises(ValueError,match='不同'):
        task_cards.check_plan(value(),task_cards.TaskPlan(action='update',target_id='a',subject_relation='different'),[record])


def test_source_reference_prevents_wrong_exact_shortcut(monkeypatch):
    a,b=SimpleNamespace(id='a'),SimpleNamespace(id='b')
    snapshots={'a':{'subject':'用户','task':'整理材料','instance':None,'sources':[],'details':[]},
               'b':{'subject':'用户','task':'整理材料','instance':'第一次','sources':[{'note_id':'prior'}],'details':[]}}
    monkeypatch.setattr(task_cards,'project',lambda r:snapshots[r.id])
    model=Mock(return_value={'action':'update','target_id':'b','subject_relation':'same','instance_relation':'same','reason':'明确引用prior'})
    monkeypatch.setattr(task_cards,'complete_json',model)
    plan=task_cards.decide(value(antecedent_note_id='prior'),[a,b])
    assert plan.target_id=='b'
    model.assert_called_once()
