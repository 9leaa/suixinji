from types import SimpleNamespace

from core import llm_client, llm_key_pool
from core.config import ChatConfig


def _response() -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


def test_key_pool_round_robin_and_cooldown(monkeypatch):
    monkeypatch.setenv("SUIXINJI_LLM_API_KEYS", "key-a,key-b")
    monkeypatch.delenv("OPENAI_API_KEYS", raising=False)
    llm_key_pool._POOLS.clear()
    pool = llm_key_pool.get_api_key_pool("chat", "legacy")
    first, _ = pool.acquire()
    second, _ = pool.acquire(exclude={first})
    assert (first, second) == ("key-a", "key-b")
    pool.report_failure(first, category="rate_limit", retry_after=20)
    assert pool.has_alternative({first})


def test_embedding_pool_does_not_consume_chat_numbered_keys(monkeypatch):
    monkeypatch.delenv("SUIXINJI_EMBEDDING_API_KEYS", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEYS", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY_1", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY_1", "chat-key-a")
    monkeypatch.setenv("OPENAI_API_KEY_2", "chat-key-b")
    llm_key_pool._POOLS.clear()

    pool = llm_key_pool.get_api_key_pool("embedding", "embedding-key")
    key, _ = pool.acquire()

    assert pool.size == 1
    assert key == "embedding-key"


def test_complete_json_fails_over_to_next_key(monkeypatch):
    monkeypatch.setenv("SUIXINJI_LLM_API_KEYS", "key-a,key-b")
    monkeypatch.delenv("OPENAI_API_KEYS", raising=False)
    llm_key_pool._POOLS.clear()
    monkeypatch.setattr(
        llm_client,
        "get_chat_config",
        lambda _role=None: ChatConfig(
            api_key="key-a",
            base_url="https://example.test/v1",
            model="test-model",
            timeout_seconds=2,
            max_retries=0,
        ),
    )
    calls = []

    class FakeCompletions:
        def __init__(self, key):
            self.key = key

        def create(self, **_kwargs):
            calls.append(self.key)
            if self.key == "key-a":
                raise RuntimeError("429 too many requests")
            return _response()

    def fake_build(config, *, api_key=None):
        key = api_key or config.api_key
        return SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(key)))

    monkeypatch.setattr(llm_client, "build_openai_client", fake_build)
    monkeypatch.setattr(llm_client, "log_event", lambda *_args, **_kwargs: None)
    assert llm_client.complete_json("system", "user") == {"ok": True}
    assert calls == ["key-a", "key-b"]
