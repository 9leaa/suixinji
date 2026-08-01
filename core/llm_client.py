"""文件作用：OpenAI-compatible LLM/embedding 客户端。

项目关系：本文件依赖 `core`、`core.config`、`core.model_router`、`core.observability` 等 7 个模块；被 `agent.query_agent`、`agent.query_intent`、`apps.handlers`、`core.classifier` 等 13 个模块。
"""



from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from openai import APITimeoutError, OpenAI

from core import settings
from core.model_router import route_model
from core.observability import log_event
from core.sensitive import safe_text_preview
from core.config import (
    ChatConfig,
    EmbeddingConfig,
    get_chat_config,
    get_embedding_config,
)

_SENSITIVE_URL_KEYS = {"api_key", "apikey", "key", "token", "secret", "access_token", "access-key", "access_key"}


def _safe_base_url(value: str | None) -> str | None:
    """函数功能：`_safe_base_url` 负责处理 safe base url，服务于本文件职责：OpenAI-compatible LLM/embedding 客户端。
    传参：
        value: 待转换、校验或计算的值，类型为 `str | None`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
    if not value:
        return value
    try:
        parts = urlsplit(value)
    except Exception:
        return "<unparseable-url>"
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    query = urlencode(
        [
            (key, "[REDACTED]" if key.strip().lower() in _SENSITIVE_URL_KEYS else item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def _is_memory_extraction_task(route: Any) -> bool:
    """函数功能：`_is_memory_extraction_task` 负责判断是否为 memory extraction task，服务于本文件职责：OpenAI-compatible LLM/embedding 客户端。
    传参：
        route: route 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    task = getattr(route, "task", None)
    return str(getattr(task, "value", task) or "") == "memory_extraction"


def _config_for_route(route: Any) -> ChatConfig:
    """函数功能：`_config_for_route` 负责路由 config for，服务于本文件职责：OpenAI-compatible LLM/embedding 客户端。
    传参：
        route: route 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `ChatConfig` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    config = get_chat_config(route.role.value)
    if not _is_memory_extraction_task(route):
        return config
    # 下方重试由该适配器显式负责；Memory 抽取关闭 SDK 重试，确保只有 APITimeoutError 会重试一次。
    return replace(
        config,
        timeout_seconds=settings.MEMORY_EXTRACTION_LLM_TIMEOUT_SECONDS,
        max_retries=0,
    )


def _timeout_retries_for_route(route: Any) -> int:
    """函数功能：`_timeout_retries_for_route` 负责路由 timeout retries for，服务于本文件职责：OpenAI-compatible LLM/embedding 客户端。
    传参：
        route: route 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `int`，表示计算得到的数值结果。
    """
    if not _is_memory_extraction_task(route):
        return 0
    return min(1, max(0, int(settings.MEMORY_EXTRACTION_LLM_MAX_RETRIES)))


def classify_llm_error(exc: BaseException | None = None, *, response: Any = None, phase: str | None = None) -> str:
    """Return a stable, low-cardinality reason for LLM failures."""
    name = type(exc).__name__.lower() if exc is not None else ""
    text = str(exc or "").lower()
    if "timeout" in name or "timeout" in text:
        return "transport_timeout"
    if any(token in name or token in text for token in ("connection", "connecterror", "network", "dns", "ssl")):
        return "connection_error"
    if any(token in name or token in text for token in ("ratelimit", "rate_limit", "429", "too many requests")):
        return "rate_limit"
    if any(token in text for token in (" 500", " 502", " 503", " 504", "server error", "bad gateway", "service unavailable")):
        return "server_error"
    if response is not None and not getattr(response, "choices", None):
        return "empty_response"
    if phase == "json" or "json" in name or "json" in text:
        if any(token in text for token in ("unterminated", "unexpected end", "truncated")):
            return "truncated_response"
        return "invalid_json"
    return "unknown"

def build_openai_client(config: ChatConfig | EmbeddingConfig | None = None) -> OpenAI:
    """函数功能：`build_openai_client` 负责构建 openai client，服务于本文件职责：OpenAI-compatible LLM/embedding 客户端。
    传参：
        config: 配置对象或配置映射，类型为 `ChatConfig | EmbeddingConfig | None`，默认值为 `None`。
    返回结果说明：
        返回 `OpenAI` 类型结果；具体字段和语义由调用方按该对象约定使用。
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
    """函数功能：`extract_json_object` 负责抽取 json object，服务于本文件职责：OpenAI-compatible LLM/embedding 客户端。
    传参：
        content: 需要处理、保存或展示的文本内容，类型为 `str`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
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


def complete_json(
    system_prompt: str,
    user_prompt: str,
    *,
    model_role: str | None = None,
    llm_task: str | None = None,
    route_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """函数功能：`complete_json` 负责完成 json，服务于本文件职责：OpenAI-compatible LLM/embedding 客户端。
    传参：
        system_prompt: system prompt 参数，由调用方传入，类型为 `str`。
        user_prompt: user prompt 参数，由调用方传入，类型为 `str`。
        model_role: model role 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        llm_task: llm task 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        route_context: route context 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    route = route_model(task=llm_task, model_role=model_role, range_key=(route_context or {}).get("range_key"))
    config = _config_for_route(route)
    client = build_openai_client(config)
    start = time.perf_counter()
    timeout_retries = _timeout_retries_for_route(route)
    max_attempts = timeout_retries + 1
    attempt = 0

    while True:
        attempt += 1
        try:
            response = client.chat.completions.create(
                model=config.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0,
            )
            break
        except APITimeoutError as exc:
            duration_ms = int((time.perf_counter() - start) * 1000)
            extra = {
                "llm_task": route.task.value,
                "model_role": route.role.value,
                "model": config.model,
                "route_reason": route.reason,
                "fallback": False,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "timeout_seconds": config.timeout_seconds,
                "error_category": classify_llm_error(exc),
            }
            if attempt < max_attempts:
                log_event(
                    "llm.complete_json",
                    level="warning",
                    status="retry",
                    duration_ms=duration_ms,
                    error=f"{type(exc).__name__}: {exc}",
                    extra={**extra, "retry_reason": "api_timeout"},
                )
                continue
            log_event(
                "llm.complete_json",
                level="error",
                status="failed",
                duration_ms=duration_ms,
                error=f"{type(exc).__name__}: {exc}",
                extra=extra,
            )
            raise RuntimeError(
                "LLM chat completion failed; "
                f"model={config.model!r}, "
                f"base_url={_safe_base_url(config.base_url)!r}, "
                f"prompt_chars={len(user_prompt)}, "
                f"attempts={attempt}, "
                f"timeout_seconds={config.timeout_seconds}, "
                f"cause={type(exc).__name__}."
            ) from None
        except Exception as exc:
            duration_ms = int((time.perf_counter() - start) * 1000)
            log_event(
                "llm.complete_json",
                level="error",
                status="failed",
                duration_ms=duration_ms,
                error=f"{type(exc).__name__}: {exc}",
                extra={
                    "llm_task": route.task.value,
                    "model_role": route.role.value,
                    "model": config.model,
                    "route_reason": route.reason,
                    "fallback": False,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "timeout_seconds": config.timeout_seconds,
                    "error_category": classify_llm_error(exc),
                },
            )
            raise RuntimeError(
                "LLM chat completion failed; "
                f"model={config.model!r}, "
                f"base_url={_safe_base_url(config.base_url)!r}, "
                f"prompt_chars={len(user_prompt)}, "
                f"attempts={attempt}, "
                f"error_category={classify_llm_error(exc)}, cause={type(exc).__name__}."
            ) from None

    if not response.choices:
        raise RuntimeError(
            "LLM returned no choices; "
            f"model={config.model!r}, "
            f"base_url={_safe_base_url(config.base_url)!r}, "
            f"prompt_chars={len(user_prompt)}, error_category={classify_llm_error(response=response)}."
        )

    usage = getattr(response, "usage", None)
    log_event(
        "llm.complete_json",
        status="success",
        duration_ms=int((time.perf_counter() - start) * 1000),
        extra={
            "llm_task": route.task.value,
            "model_role": route.role.value,
            "model": config.model,
            "route_reason": route.reason,
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "fallback": False,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "timeout_seconds": config.timeout_seconds,
        },
    )
    content = response.choices[0].message.content

    if content is None:
        raise RuntimeError(
            "LLM returned no message content; "
            f"model={config.model!r}, "
            f"base_url={_safe_base_url(config.base_url)!r}, "
            f"prompt_chars={len(user_prompt)}, error_category=empty_response."
        )

    try:
        return extract_json_object(content)
    except Exception as exc:
        output_preview = safe_text_preview(content, limit=200)

        raise RuntimeError(
            "LLM did not return valid JSON object; "
            f"model={config.model!r}, "
            f"base_url={_safe_base_url(config.base_url)!r}, "
            f"prompt_chars={len(user_prompt)}, "
            f"output_preview={output_preview!r}, error_category={classify_llm_error(exc, phase='json')}."
        ) from None


def embed_text(text: str) -> list[float]:
    """函数功能：`embed_text` 负责生成向量 text，服务于本文件职责：OpenAI-compatible LLM/embedding 客户端。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `list[float]`，表示按条件筛选、构造或查询得到的列表。
    """
    if not text or not text.strip():
        raise ValueError("embed_text received empty text")

    config = get_embedding_config()
    normalized_text = " ".join(text.split())
    cache = None
    try:
        from core.settings import CACHE_ENABLED, COORDINATION_BACKEND
        if CACHE_ENABLED and COORDINATION_BACKEND == "redis":
            from infrastructure.redis_cache import EmbeddingCache
            cache = EmbeddingCache()
            cached = cache.get(config.model, normalized_text)
            if cached is not None and len(cached) == config.dimension:
                return cached
    except Exception:
        cache = None
    client = build_openai_client(config)

    try:
        response = client.embeddings.create(
            model=config.model,
            input=normalized_text,
            dimensions=config.dimension,
            encoding_format="float",
        )
    except Exception as exc:
        preview = safe_text_preview(text)

        raise RuntimeError(
            "Embedding request failed; "
            f"model={config.model!r}, "
            f"base_url={config.base_url!r}, "
            f"dimension={config.dimension!r}, "
            f"text_preview={preview!r}, "
            f"cause={type(exc).__name__}."
        ) from None

    if not response.data:
        preview = safe_text_preview(text)
        raise RuntimeError(
            "Embedding response contains no data; "
            f"model={config.model!r}, "
            f"base_url={config.base_url!r}, "
            f"text_preview={preview!r}."
        )

    embedding = response.data[0].embedding

    if not embedding:
        preview = safe_text_preview(text)
        raise RuntimeError(
            "Embedding response contains empty embedding; "
            f"model={config.model!r}, "
            f"base_url={config.base_url!r}, "
            f"text_preview={preview!r}."
        )

    if cache is not None:
        try:
            cache.set(config.model, normalized_text, embedding)
        except Exception:
            pass
    return embedding
