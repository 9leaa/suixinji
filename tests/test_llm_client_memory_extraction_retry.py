from types import SimpleNamespace

import pytest

from core import llm_client
from core.config import ChatConfig


class FakeTimeout(Exception):
    pass


def _response(content: str = '{"ok": true}') -> SimpleNamespace:
    """验证“response”场景的预期行为与回归边界。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


def _install_fake_config(monkeypatch) -> list[ChatConfig]:
    """验证“installfake配置”场景的预期行为与回归边界。"""
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
    """验证“记忆extractionusesdedicated超时andretriesone超时”场景的预期行为与回归边界。"""
    configs = _install_fake_config(monkeypatch)
    events = []
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            """验证“创建”场景的预期行为与回归边界。"""
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
    """验证“记忆extraction超时重试是否为cappedatone”场景的预期行为与回归边界。"""
    configs = _install_fake_config(monkeypatch)
    events = []
    calls = []
    monkeypatch.setattr(llm_client.settings, "MEMORY_EXTRACTION_LLM_MAX_RETRIES", 10)

    class FakeCompletions:
        def create(self, **kwargs):
            """验证“创建”场景的预期行为与回归边界。"""
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
    """验证“超时重试doesnotapply转换为otherLLMtasks”场景的预期行为与回归边界。"""
    configs = _install_fake_config(monkeypatch)
    events = []
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            """验证“创建”场景的预期行为与回归边界。"""
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
