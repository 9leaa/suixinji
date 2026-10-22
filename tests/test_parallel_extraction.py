from contextvars import ContextVar
from threading import Barrier
import json

import pytest
from pydantic import ValidationError

from memory.parallel_extraction import extract_parallel


def test_parallel_calls_preserve_context_and_disjoint_contracts():
    barrier = Barrier(2)
    marker = ContextVar('test_trace', default=None)
    token = marker.set('same-parent')
    seen = []
    def complete(**kw):
        assert marker.get() == 'same-parent'
        barrier.wait(timeout=3)  # A sequential implementation cannot pass.
        schema = json.dumps(kw['response_schema'])
        seen.append(kw)
        if kw.get('llm_task'):
            assert 'semantic' not in schema and 'episodic' not in schema
            return {'candidates': [{'memory_type': 'task', 'task': '上课',
                'task_status': 'todo', 'evidence_span': '今天上课'}]}
        assert 'canonical_topic' not in schema and 'new_value' not in schema
        assert 'JSON Schema:' in kw['system_prompt']
        assert 'subject/entity 统一写' not in kw['system_prompt']
        return {'candidates': [{'memory_type': 'semantic', 'content': '信号弱', 'evidence_span': '信号弱'}]}
    try:
        rows, _ = extract_parallel(complete, {'text': '今天上课，信号弱'}, [], '')
    finally:
        marker.reset(token)
    assert len(seen) == 2
    assert [r['memory_type'] for r in rows] == ['task', 'semantic']


def test_wrong_branch_is_rejected_not_overwritten():
    def complete(**kw):
        return {'candidates': [{'memory_type': 'semantic', 'content': '信号弱', 'evidence_span': '信号弱'}]}
    with pytest.raises(ValidationError):
        extract_parallel(complete, {'text': '信号弱'}, [], '')


def test_branch_failure_does_not_return_partial_candidates():
    def complete(**kw):
        if not kw.get('llm_task'):
            raise RuntimeError('fact request failed')
        return {'candidates': []}
    with pytest.raises(RuntimeError, match='fact request failed'):
        extract_parallel(complete, {'text': '消息'}, [], '')
