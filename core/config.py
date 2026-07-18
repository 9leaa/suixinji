"""Project configuration helpers loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from core.settings import EMBEDDING_TIMEOUT_SECONDS, LLM_MAX_RETRIES, LLM_TIMEOUT_SECONDS

load_dotenv()


@dataclass(frozen=True)
class ChatConfig:
    """LLM provider configuration.

    功能说明:
        保存 OpenAI 或 OpenAI-compatible 服务所需的基础配置，避免业务模块直接读取环境变量。

    传参说明:
        api_key: LLM 服务 API key，可为空；为空时交给 OpenAI SDK 默认环境变量处理。
        base_url: LLM 服务地址；为空时使用 OpenAI SDK 默认官方地址。
        model: 调用的模型名称。

    返回类型说明:
        LLMConfig: 一份不可变的 LLM 配置对象。
    """

    api_key: str | None
    base_url: str | None
    model: str
    timeout_seconds: int
    max_retries: int

@dataclass
class EmbeddingConfig:
    api_key: str | None
    base_url:str | None
    model:str
    dimension: int = 1024
    timeout_seconds: int = 20
    max_retries: int = 2


def get_chat_config(model_role: str | None = None) -> ChatConfig:
    """读取当前 LLM 配置。

    功能说明:
        从 `.env` 或系统环境变量读取 OPENAI_API_KEY、OPENAI_BASE_URL、OPENAI_MODEL，
        并整理成统一的 LLMConfig。

    传参说明:
        无参数。

    返回类型说明:
        LLMConfig: 当前进程使用的 LLM 配置。
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
    return EmbeddingConfig(
        api_key=os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY") or None,
        base_url=os.getenv("EMBEDDING_BASE_URL") or os.getenv("OPENAI_BASE_URL") or None,
        model=os.getenv("EMBEDDING_MODEL", "text-embedding-v3"),
        dimension=int(os.getenv("EMBEDDING_DIMENSION", "1024")),
        timeout_seconds=EMBEDDING_TIMEOUT_SECONDS,
        max_retries=LLM_MAX_RETRIES,
    )
