from core.llm_client import classify_llm_error


def test_classify_llm_error_categories_are_stable():
    assert classify_llm_error(TimeoutError("deadline")) == "transport_timeout"
    assert classify_llm_error(ConnectionError("network down")) == "connection_error"
    assert classify_llm_error(RuntimeError("HTTP 429 too many requests")) == "rate_limit"
    assert classify_llm_error(RuntimeError("HTTP 503 service unavailable")) == "server_error"
    assert classify_llm_error(RuntimeError("Expecting value: line 1"), phase="json") == "invalid_json"
