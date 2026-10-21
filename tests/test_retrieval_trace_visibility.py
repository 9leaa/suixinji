from types import SimpleNamespace
from unittest.mock import Mock

from core import lifecycle
from memory import fact_memory, fact_retrieval


def test_supplemental_retrieval_traces_low_score_and_limit(monkeypatch):
    spans = []
    monkeypatch.setattr(lifecycle, '_save', spans.append)
    monkeypatch.setattr(fact_retrieval.settings, 'FACT_MEMORY_SEMANTIC_SEARCH_ENABLED', True)
    monkeypatch.setattr(fact_retrieval, 'get_embedding_config', lambda: SimpleNamespace(base_url='test', model='test', dimension=2))
    monkeypatch.setattr(fact_retrieval, 'embed_text', lambda text: [1., 0.] if text == 'question' else [.5, .8660254037844386])
    monkeypatch.setattr(fact_memory, 'score', lambda *args: 0.)
    monkeypatch.setattr(fact_memory, 'get', lambda *args: None)
    monkeypatch.setattr(fact_memory.store, 'put', Mock())
    rows = [{'id': 'one', 'content': 'first'}, {'id': 'two', 'content': 'second'}]
    with lifecycle.resume({'trace_id': 'test-retrieval'}):
        result = fact_retrieval.rank('s', 'question', rows, 1, stage='合并候选评分与截断')
    assert len(result) == 1 and result[0]['score'] < .45
    span = next(s for s in spans if s['name'] == '记忆 · 补充检索与评分')
    assert span['output_json'] == result
    meta = span['metadata_json']
    assert meta['input_count'] == 2 and meta['output_count'] == 1
    assert meta['decisions'][0]['retained'] is True
    assert meta['decisions'][1]['reason'] == '通过候选过滤，因数量限制截断'


def test_supplemental_retrieval_traces_empty_result(monkeypatch):
    spans = []
    monkeypatch.setattr(lifecycle, '_save', spans.append)
    monkeypatch.setattr(fact_retrieval.settings, 'FACT_MEMORY_SEMANTIC_SEARCH_ENABLED', False)
    monkeypatch.setattr(fact_memory, 'score', lambda *args: 0.)
    monkeypatch.setattr(fact_memory.store, 'put', Mock())
    with lifecycle.resume({'trace_id': 'test-empty'}):
        assert fact_retrieval.rank('s', 'question', [{'id': 'one', 'content': 'unrelated'}], 5) == []
    assert spans[-1]['metadata_json']['decisions'][0]['reason'] == '未满足候选保留条件'
