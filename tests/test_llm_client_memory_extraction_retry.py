"""文件作用：抽取专用 30 秒超时、仅 timeout 一次重试及脱敏日志。

项目关系：本文件依赖 `core`、`core.config`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from types import SimpleNamespace

import pytest

from core import llm_client
from core.config import ChatConfig


class FakeTimeout(Exception):
    """类功能：`FakeTimeout` 封装与“抽取专用 30 秒超时、仅 timeout 一次重试及脱敏日志”相关的数据结构、状态或行为。
    继承关系：继承 `Exception`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    pass


def _response(content: str = '{"ok": true}') -> SimpleNamespace:
    """函数功能：`_response` 负责处理 response，服务于本文件职责：抽取专用 30 秒超时、仅 timeout 一次重试及脱敏日志。
    传参：
        content: 需要处理、保存或展示的文本内容，类型为 `str`，默认值为 `'{"ok": true}'`。
    返回结果说明：
        返回 `SimpleNamespace` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


def _install_fake_config(monkeypatch) -> list[ChatConfig]:
    """函数功能：`_install_fake_config` 负责处理 install fake config，服务于本文件职责：抽取专用 30 秒超时、仅 timeout 一次重试及脱敏日志。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回 `list[ChatConfig]`，表示按条件筛选、构造或查询得到的列表。
    """
    configs: list[ChatConfig] = []

    monkeypatch.setattr(llm_client, "APITimeoutError", FakeTimeout)
    monkeypatch.setattr(
        llm_client,
        "get_chat_config",
        lambda _role: ChatConfig(
            api_key="sk-test",
            base_url="https://example.test/v1?api_key=sk-secretsecretsecret",
            model="fast-model",
            timeout_seconds=15,
            max_retries=2,
        ),
    )
    monkeypatch.setattr(llm_client.settings, "MEMORY_EXTRACTION_LLM_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(llm_client.settings, "MEMORY_EXTRACTION_LLM_MAX_RETRIES", 1)
    return configs


def test_memory_extraction_uses_dedicated_timeout_and_retries_one_timeout(monkeypatch):
    """函数功能：`test_memory_extraction_uses_dedicated_timeout_and_retries_one_timeout` 负责验证 memory extraction uses dedicated timeout and retries one timeout 场景，服务于本文件职责：抽取专用 30 秒超时、仅 timeout 一次重试及脱敏日志。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    configs = _install_fake_config(monkeypatch)
    events = []
    calls = []

    class FakeCompletions:
        """类功能：`FakeCompletions` 封装与“抽取专用 30 秒超时、仅 timeout 一次重试及脱敏日志”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def create(self, **kwargs):
            """函数功能：`FakeCompletions.create` 在类 `FakeCompletions` 中负责创建，服务于本文件职责：抽取专用 30 秒超时、仅 timeout 一次重试及脱敏日志。
            传参：
                **kwargs: kwargs 参数，由调用方传入。
            返回结果说明：
                返回计算后的结果对象；具体类型取决于实际执行分支。
            """
            calls.append(kwargs)
            if len(calls) == 1:
                raise FakeTimeout("Request timed out.")
            return _response()

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    monkeypatch.setattr(llm_client, "build_openai_client", lambda config: configs.append(config) or fake_client)
    monkeypatch.setattr(llm_client, "log_event", lambda action, **kwargs: events.append((action, kwargs)))

    data = llm_client.complete_json(
        system_prompt="Return JSON.",
        user_prompt='{"input":"hello"}',
        llm_task="memory_extraction",
    )

    assert data == {"ok": True}
    assert len(calls) == 2
    assert configs[0].timeout_seconds == 30
    assert configs[0].max_retries == 0
    retry_events = [event for event in events if event[1]["status"] == "retry"]
    assert len(retry_events) == 1
    assert retry_events[0][1]["extra"]["retry_reason"] == "api_timeout"
    assert retry_events[0][1]["extra"]["max_attempts"] == 2
    assert events[-1][1]["status"] == "success"
    assert events[-1][1]["extra"]["attempt"] == 2


def test_memory_extraction_timeout_retry_is_capped_at_one(monkeypatch):
    """函数功能：`test_memory_extraction_timeout_retry_is_capped_at_one` 负责验证 memory extraction timeout retry is capped at one 场景，服务于本文件职责：抽取专用 30 秒超时、仅 timeout 一次重试及脱敏日志。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    configs = _install_fake_config(monkeypatch)
    events = []
    calls = []
    monkeypatch.setattr(llm_client.settings, "MEMORY_EXTRACTION_LLM_MAX_RETRIES", 10)

    class FakeCompletions:
        """类功能：`FakeCompletions` 封装与“抽取专用 30 秒超时、仅 timeout 一次重试及脱敏日志”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def create(self, **kwargs):
            """函数功能：`FakeCompletions.create` 在类 `FakeCompletions` 中负责创建，服务于本文件职责：抽取专用 30 秒超时、仅 timeout 一次重试及脱敏日志。
            传参：
                **kwargs: kwargs 参数，由调用方传入。
            返回结果说明：
                无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
            """
            calls.append(kwargs)
            raise FakeTimeout("Request timed out.")

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    monkeypatch.setattr(llm_client, "build_openai_client", lambda config: configs.append(config) or fake_client)
    monkeypatch.setattr(llm_client, "log_event", lambda action, **kwargs: events.append((action, kwargs)))

    with pytest.raises(RuntimeError) as exc_info:
        llm_client.complete_json(
            system_prompt="Return JSON.",
            user_prompt='{"input":"hello"}',
            llm_task="memory_extraction",
        )

    assert len(calls) == 2
    assert "attempts=2" in str(exc_info.value)
    assert "api_key=sk-secretsecretsecret" not in str(exc_info.value)
    assert events[-1][1]["status"] == "failed"
    assert events[-1][1]["extra"]["max_attempts"] == 2


def test_timeout_retry_does_not_apply_to_other_llm_tasks(monkeypatch):
    """函数功能：`test_timeout_retry_does_not_apply_to_other_llm_tasks` 负责验证 timeout retry does not apply to other llm tasks 场景，服务于本文件职责：抽取专用 30 秒超时、仅 timeout 一次重试及脱敏日志。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    configs = _install_fake_config(monkeypatch)
    events = []
    calls = []

    class FakeCompletions:
        """类功能：`FakeCompletions` 封装与“抽取专用 30 秒超时、仅 timeout 一次重试及脱敏日志”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def create(self, **kwargs):
            """函数功能：`FakeCompletions.create` 在类 `FakeCompletions` 中负责创建，服务于本文件职责：抽取专用 30 秒超时、仅 timeout 一次重试及脱敏日志。
            传参：
                **kwargs: kwargs 参数，由调用方传入。
            返回结果说明：
                无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
            """
            calls.append(kwargs)
            raise FakeTimeout("Request timed out.")

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    monkeypatch.setattr(llm_client, "build_openai_client", lambda config: configs.append(config) or fake_client)
    monkeypatch.setattr(llm_client, "log_event", lambda action, **kwargs: events.append((action, kwargs)))

    with pytest.raises(RuntimeError):
        llm_client.complete_json(
            system_prompt="Return JSON.",
            user_prompt="hello",
            llm_task="note_classification",
        )

    assert len(calls) == 1
    assert configs[0].timeout_seconds == 15
    assert configs[0].max_retries == 2
    assert not [event for event in events if event[1]["status"] == "retry"]
