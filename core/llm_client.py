"""LLM client adapter for OpenAI and OpenAI-compatible providers."""

from __future__ import annotations

import json
import re
from typing import Any

from openai import OpenAI

from core.config import (
    ChatConfig,
    EmbeddingConfig,
    get_chat_config,
    get_embedding_config,
)


def build_openai_client(config: ChatConfig | EmbeddingConfig | None = None) -> OpenAI:
    """创建 OpenAI SDK client。

    功能说明:
        根据 ChatConfig 或 EmbeddingConfig 创建 OpenAI client。

        - ChatConfig 通常用于聊天 / 分类模型，例如 cc-switch。
        - EmbeddingConfig 通常用于 embedding 模型，例如阿里云百炼 OpenAI-compatible 接口。

        如果 config 为空，默认读取 chat 配置。

    传参说明:
        config: 可选配置对象。可以是 ChatConfig 或 EmbeddingConfig。

    返回类型说明:
        OpenAI: 已配置好的 OpenAI SDK client。
    """
    config = config or get_chat_config()

    kwargs: dict[str, Any] = {}

    if config.api_key:
        kwargs["api_key"] = config.api_key

    if config.base_url:
        kwargs["base_url"] = config.base_url

    if config.timeout_seconds:
        kwargs["timeout"] = config.timeout_seconds

    if config.max_retries is not None:
        kwargs["max_retries"] = config.max_retries

    return OpenAI(**kwargs)


def extract_json_object(content: str) -> dict[str, Any]:
    """从模型输出文本中提取 JSON object。

    功能说明:
        兼容模型直接输出 JSON、输出 ```json fenced block```、
        或在解释文本中夹带 JSON 的情况。

        该函数只接受 JSON object，不接受 JSON array 或普通字符串。

    传参说明:
        content: 模型返回的原始文本。

    返回类型说明:
        dict[str, Any]: 解析后的 JSON object。
    """
    content = content.strip()

    if not content:
        raise ValueError("LLM returned empty content")

    fence_match = re.search(
        r"```(?:json)?\s*(.*?)```",
        content,
        re.DOTALL | re.IGNORECASE,
    )

    if fence_match:
        content = fence_match.group(1).strip()

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")

        if start == -1 or end == -1 or end <= start:
            raise

        data = json.loads(content[start : end + 1])

    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object, got {type(data).__name__}")

    return data


def complete_json(system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """调用 Chat Completions 并返回 JSON object。

    功能说明:
        使用 Chat Completions 进行普通文本调用，要求模型返回 JSON，
        再在本地解析为 dict。

        这种方式比 Responses API structured parse 更兼容本地代理和
        OpenAI-compatible 服务。

    传参说明:
        system_prompt: 系统提示词。
        user_prompt: 用户输入提示词。

    返回类型说明:
        dict[str, Any]: 从模型输出中解析出的 JSON object。
    """
    config = get_chat_config()
    client = build_openai_client(config)

    try:
        response = client.chat.completions.create(
            model=config.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
        )
    except Exception as exc:
        preview = user_prompt.replace("\n", " ")[:80]
        raise RuntimeError(
            "LLM chat completion failed; "
            f"model={config.model!r}, "
            f"base_url={config.base_url!r}, "
            f"text_preview={preview!r}."
        ) from exc

    if not response.choices:
        preview = user_prompt.replace("\n", " ")[:80]
        raise RuntimeError(
            "LLM returned no choices; "
            f"model={config.model!r}, "
            f"base_url={config.base_url!r}, "
            f"text_preview={preview!r}."
        )

    content = response.choices[0].message.content

    if content is None:
        preview = user_prompt.replace("\n", " ")[:80]
        raise RuntimeError(
            "LLM returned no message content; "
            f"model={config.model!r}, "
            f"base_url={config.base_url!r}, "
            f"text_preview={preview!r}."
        )

    try:
        return extract_json_object(content)
    except Exception as exc:
        preview = user_prompt.replace("\n", " ")[:80]
        output_preview = content.replace("\n", " ")[:200]

        raise RuntimeError(
            "LLM did not return valid JSON object; "
            f"model={config.model!r}, "
            f"base_url={config.base_url!r}, "
            f"text_preview={preview!r}, "
            f"output_preview={output_preview!r}."
        ) from exc


def embed_text(text: str) -> list[float]:
    """生成单段文本的 embedding 向量。

    功能说明:
        调用 OpenAI-compatible embeddings API，
        将文本转换成向量，供 P2 语义检索和相关笔记推荐使用。

        当前建议用于：
        - 阿里云百炼 text-embedding-v4
        - OpenAI text-embedding-3-small / large
        - 其他兼容 /v1/embeddings 的服务

    传参说明:
        text: 需要生成 embedding 的原始文本。

    返回类型说明:
        list[float]: 文本对应的 embedding 向量。
    """
    if not text or not text.strip():
        raise ValueError("embed_text received empty text")

    config = get_embedding_config()
    client = build_openai_client(config)

    try:
        response = client.embeddings.create(
            model=config.model,
            input=text,
            dimensions=config.dimension,
            encoding_format="float",
        )
    except Exception as exc:
        preview = text.replace("\n", " ")[:80]

        raise RuntimeError(
            "Embedding request failed; "
            f"model={config.model!r}, "
            f"base_url={config.base_url!r}, "
            f"dimension={config.dimension!r}, "
            f"text_preview={preview!r}."
        ) from exc

    if not response.data:
        preview = text.replace("\n", " ")[:80]
        raise RuntimeError(
            "Embedding response contains no data; "
            f"model={config.model!r}, "
            f"base_url={config.base_url!r}, "
            f"text_preview={preview!r}."
        )

    embedding = response.data[0].embedding

    if not embedding:
        preview = text.replace("\n", " ")[:80]
        raise RuntimeError(
            "Embedding response contains empty embedding; "
            f"model={config.model!r}, "
            f"base_url={config.base_url!r}, "
            f"text_preview={preview!r}."
        )

    return embedding
