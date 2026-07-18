from __future__ import annotations

from types import SimpleNamespace

from agent import query_agent
from core import llm_client, settings
from infrastructure import redis_cache


def test_common_query_fast_path_coverage_is_at_least_70_percent() -> None:
    common_queries = (
        "/type 学习",
        "/tag 饮食",
        "最近一周记了什么",
        "我现在喜欢喝什么",
        "我讨厌吃什么",
        "当前待办是什么",
        "我现在住在哪里",
        "上次记录的数据库索引是什么",
        "找一下项目部署记录",
        "比较最近三个月的学习和任务变化趋势",
    )
    routed = [query for query in common_queries if query_agent._deterministic_route(query) is not None]
    assert len(routed) / len(common_queries) >= 0.7
    assert query_agent._deterministic_route(common_queries[-1]) is None


def test_embedding_cache_avoids_duplicate_external_call(monkeypatch) -> None:
    store: dict[tuple[str, str], list[float]] = {}
    external_calls = []

    class FakeEmbeddingCache:
        def get(self, model: str, text: str):
            return store.get((model, text))

        def set(self, model: str, text: str, embedding: list[float]):
            store[(model, text)] = embedding

    class FakeEmbeddings:
        def create(self, **kwargs):
            external_calls.append(kwargs)
            return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3])])

    monkeypatch.setattr(settings, "COORDINATION_BACKEND", "redis")
    monkeypatch.setattr(settings, "CACHE_ENABLED", True)
    monkeypatch.setattr(redis_cache, "EmbeddingCache", FakeEmbeddingCache)
    monkeypatch.setattr(
        llm_client,
        "get_embedding_config",
        lambda: SimpleNamespace(model="embedding-test", dimension=3, base_url="", api_key="", timeout_seconds=1, max_retries=0),
    )
    monkeypatch.setattr(llm_client, "build_openai_client", lambda config: SimpleNamespace(embeddings=FakeEmbeddings()))

    first = llm_client.embed_text("  相同   查询 ")
    second = llm_client.embed_text("相同 查询")

    assert first == second == [0.1, 0.2, 0.3]
    assert len(external_calls) == 1
