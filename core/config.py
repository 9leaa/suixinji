"""文件作用：LLM 与 embedding provider 配置。

项目关系：本文件依赖 `core.settings`；被 `agent.hooks.llm_usage`、`core.llm_client`、`eval.large_live_retrieval_eval`、`eval.live_retrieval_eval` 等 10 个模块。
"""



from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from core.settings import EMBEDDING_TIMEOUT_SECONDS, LLM_MAX_RETRIES, LLM_TIMEOUT_SECONDS

load_dotenv()


@dataclass(frozen=True)
class ChatConfig:
    """类功能：`ChatConfig` 封装与“LLM 与 embedding provider 配置”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """

    api_key: str | None
    base_url: str | None
    model: str
    timeout_seconds: int
    max_retries: int

@dataclass
class EmbeddingConfig:
    """类功能：`EmbeddingConfig` 封装与“LLM 与 embedding provider 配置”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    api_key: str | None
    base_url:str | None
    model:str
    dimension: int = 1024
    timeout_seconds: int = 20
    max_retries: int = 2


def get_chat_config(model_role: str | None = None) -> ChatConfig:
    """函数功能：`get_chat_config` 负责获取 chat config，服务于本文件职责：LLM 与 embedding provider 配置。
    传参：
        model_role: model role 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `ChatConfig` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    role_models = {
        "fast": os.getenv("SUIXINJI_FAST_MODEL", "gpt-5.4-mini"),
        "balanced": os.getenv("SUIXINJI_BALANCED_MODEL", "gpt-5.4"),
        "strong": os.getenv("SUIXINJI_STRONG_MODEL", "gpt-5.5"),
    }
    model = role_models.get(str(model_role or "").strip().lower()) or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    return ChatConfig(
        api_key=os.getenv("OPENAI_API_KEY") or None,
        base_url=os.getenv("OPENAI_BASE_URL") or None,
        model=model,
        timeout_seconds=LLM_TIMEOUT_SECONDS,
        max_retries=LLM_MAX_RETRIES,
    )

def get_embedding_config() -> EmbeddingConfig:
    """函数功能：`get_embedding_config` 负责获取 embedding config，服务于本文件职责：LLM 与 embedding provider 配置。
    传参：
        无。
    返回结果说明：
        返回 `EmbeddingConfig` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return EmbeddingConfig(
        api_key=os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY") or None,
        base_url=os.getenv("EMBEDDING_BASE_URL") or os.getenv("OPENAI_BASE_URL") or None,
        model=os.getenv("EMBEDDING_MODEL", "text-embedding-v3"),
        dimension=int(os.getenv("EMBEDDING_DIMENSION", "1024")),
        timeout_seconds=EMBEDDING_TIMEOUT_SECONDS,
        max_retries=LLM_MAX_RETRIES,
    )
