import json
from unittest.mock import Mock

import pytest

from core import settings, llm_client
from core.config import ChatConfig
from memory import extractor
from memory.extraction_contract import extraction_schema, extraction_prompt, strict_schema, repair_task_row
from memory.task_schema import TaskInput


def task():
    return {'memory_type':'task','subject':'用户','task':'提交周报','task_status':'todo','description':'提交周报', 'evidence_span':'提交周报'}


@pytest.mark.parametrize('preferences,v3', [(False,False),(False,True),(True,False),(True,True)])
def test_task_has_one_contract_without_legacy_fields(preferences,v3):
    schema=extraction_schema(preferences=preferences,v3=v3)
    branches=schema['properties']['candidates']['items']['anyOf']
    task_defs=[schema['$defs'][b['$ref'].split('/')[-1]] for b in branches if b['$ref'].endswith('/TaskInput')]
    assert len(task_defs)==1
    assert set(task_defs[0]['properties'])==set(TaskInput.model_fields)
    assert task_defs[0]['additionalProperties'] is False
    for b in branches:
        if not b['$ref'].endswith('/TaskInput'):
            other=schema['$defs'][b['$ref'].split('/')[-1]]['properties']['memory_type']
            assert 'task' not in other.get('enum',[])
    prompt=extraction_prompt(schema,preferences=preferences,v3=v3)
    assert 'task 必须有 entity' not in prompt
    assert '替代前面的旧 task 字段规则' not in prompt
    strict=strict_schema(schema)
    assert 'default' not in json.dumps(strict)
    assert set(strict['$defs']['TaskInput']['required'])==set(TaskInput.model_fields)


def test_extra_field_repair_preserves_original_and_all_valid_values():
    original={**task(), 'entities':['周报']}
    fixed=task()
    mock=Mock(return_value=fixed)
    got,audit=repair_task_row(original,complete=mock,context={'text':'提交周报','previous_messages':[]})
    assert got==fixed and audit['original']==original and audit['status']=='repaired'
    mock.assert_called_once()


@pytest.mark.parametrize('failure',['still_invalid','changes_status','api_failure'])
def test_repair_failure_retains_invalid_candidate_for_pending(failure):
    original={**task(),'entities':['周报']}
    fixed=task()
    if failure=='still_invalid':
        fixed=original
    if failure=='changes_status':
        fixed['task_status']='done'
    mock=Mock(side_effect=RuntimeError('provider failed')) if failure=='api_failure' else Mock(return_value=fixed)
    got,audit=repair_task_row(original,complete=mock,context={'text':'提交周报','previous_messages':[]})
    assert got==original and audit['status']=='failed'
    mock.assert_called_once()


def test_business_errors_do_not_trigger_format_retry():
    mock=Mock()
    bad={**task(),'task_status':'blocked'}
    got,audit=repair_task_row(bad,complete=mock,context={})
    assert got==bad and audit is None
    mock.assert_not_called()


def test_mixed_extraction_keeps_valid_sibling_and_audits_repair(monkeypatch):
    monkeypatch.setattr(settings,'TASK_CARDS_ENABLED',True)
    monkeypatch.setattr(extractor,'MEMORY_EXTRACTOR_MODE','llm')
    bad={**task(),'entities':['周报']}
    other={'memory_type':'semantic','entity':'用户','attribute':'location','new_value':'北京','content':'用户住在北京',
           'evidence_span':'我住在北京','confidence':.9,'importance':.6}
    call=Mock(side_effect=[{'candidates':[bad,other]},task()])
    monkeypatch.setattr(extractor,'complete_json',call)
    candidates=extractor.extract_llm_candidates('n','提交周报；我住在北京')
    assert {c.memory_type for c in candidates}=={'task','semantic'}
    candidate=next(c for c in candidates if c.memory_type=='task')
    assert candidate.scope['task_schema_repair']['original']==bad
    assert 'task_error' not in candidate.scope
    assert call.call_count==2


def test_provider_schema_options_are_explicit():
    schema=strict_schema(TaskInput.model_json_schema())
    deepseek=ChatConfig('test','https://api.deepseek.com','deepseek-flash',30,0)
    supported=ChatConfig('test','https://api.openai.com/v1','example',30,0)
    assert llm_client._structured_json_completion_options(deepseek,schema)['response_format']['type']=='json_object'
    request=llm_client._structured_json_completion_options(supported,schema)['response_format']
    assert request['type']=='json_schema' and request['json_schema']['strict'] is True
    assert request['json_schema']['schema']==schema
