"""文件作用：Stage 2 查询性能指标。

项目关系：本文件依赖 `agent`、`core`、`infrastructure`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from __future__ import annotations

from types import SimpleNamespace

from agent import query_agent
from core import llm_client, settings
from infrastructure import redis_cache


def test_common_query_fast_path_coverage_is_at_least_70_percent() -> None:
    """函数功能：`test_common_query_fast_path_coverage_is_at_least_70_percent` 负责验证 common query fast path coverage is at least 70 percent 场景，服务于本文件职责：Stage 2 查询性能指标。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`test_embedding_cache_avoids_duplicate_external_call` 负责验证 embedding cache avoids duplicate external call 场景，服务于本文件职责：Stage 2 查询性能指标。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    store: dict[tuple[str, str], list[float]] = {}
    external_calls = []

    class FakeEmbeddingCache:
        """类功能：`FakeEmbeddingCache` 封装与“Stage 2 查询性能指标”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def get(self, model: str, text: str):
            """函数功能：`FakeEmbeddingCache.get` 在类 `FakeEmbeddingCache` 中负责获取，服务于本文件职责：Stage 2 查询性能指标。
            传参：
                model: model 参数，由调用方传入，类型为 `str`。
                text: 输入文本内容，类型为 `str`。
            返回结果说明：
                返回计算后的结果对象；具体类型取决于实际执行分支。
            """
            return store.get((model, text))

        def set(self, model: str, text: str, embedding: list[float]):
            """函数功能：`FakeEmbeddingCache.set` 在类 `FakeEmbeddingCache` 中负责设置，服务于本文件职责：Stage 2 查询性能指标。
            传参：
                model: model 参数，由调用方传入，类型为 `str`。
                text: 输入文本内容，类型为 `str`。
                embedding: embedding 参数，由调用方传入，类型为 `list[float]`。
            返回结果说明：
                无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
            """
            store[(model, text)] = embedding

    class FakeEmbeddings:
        """类功能：`FakeEmbeddings` 封装与“Stage 2 查询性能指标”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def create(self, **kwargs):
            """函数功能：`FakeEmbeddings.create` 在类 `FakeEmbeddings` 中负责创建，服务于本文件职责：Stage 2 查询性能指标。
            传参：
                **kwargs: kwargs 参数，由调用方传入。
            返回结果说明：
                返回计算后的结果对象；具体类型取决于实际执行分支。
            """
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
