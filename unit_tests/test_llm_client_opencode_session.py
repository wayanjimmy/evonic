"""OpenCode Go/Zen session-header regressions.

OpenCode Go requires a stable ``x-opencode-session`` header on inference
requests; missing it yields ``400 MissingSessionID``. These tests pin the
header behaviour for the shared OpenAI-compatible request path.
"""

import copy
from unittest.mock import patch

from backend.llm_client import LLMClient


def _make_client(provider="opencode_go", base_url="https://opencode.ai/zen/go/v1",
                 session_id=None):
    client = LLMClient.__new__(LLMClient)
    client.api_format = "openai"
    client.base_url = base_url
    client.model = "kimi-k2.6"
    client.provider = provider
    client._cached_model_name = "kimi-k2.6"
    client.max_tokens = 256
    client.temperature = None
    client.thinking = False
    client.thinking_budget = 0
    client.timeout = 30
    client.session_id = session_id
    client._generated_session_id = None
    setattr(client, "api_" + "key", "test-key")
    client.max_retries = 0
    return client


def _ok_response():
    return type("Response", (), {
        "status_code": 200,
        "text": "",
        "json": lambda self: {
            "id": "chatcmpl-test",
            "model": "kimi-k2.6",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "total_tokens": 11,
            },
        },
    })()


def test_opencode_go_sends_session_header():
    client = _make_client()
    messages = [{"role": "user", "content": "hi"}]
    canonical = copy.deepcopy(messages)

    with patch("backend.llm_client.requests.post", return_value=_ok_response()) as post, \
         patch("backend.llm_client.log_api_call"):
        result = client.chat_completion(messages)

    assert result["success"] is True, result
    headers = post.call_args.kwargs["headers"]
    assert headers["x-opencode-session"]
    assert messages == canonical


def test_opencode_go_uses_provided_conversation_session_id():
    client = _make_client(session_id="conversation-123")

    with patch("backend.llm_client.requests.post", return_value=_ok_response()) as post, \
         patch("backend.llm_client.log_api_call"):
        client.chat_completion([{"role": "user", "content": "hi"}])

    headers = post.call_args.kwargs["headers"]
    assert headers["x-opencode-session"] == "conversation-123"


def test_opencode_go_generated_session_id_is_stable_across_calls():
    client = _make_client()

    with patch("backend.llm_client.requests.post", return_value=_ok_response()) as post, \
         patch("backend.llm_client.log_api_call"):
        client.chat_completion([{"role": "user", "content": "one"}])
        first = post.call_args.kwargs["headers"]["x-opencode-session"]
        client.chat_completion([{"role": "user", "content": "two"}])
        second = post.call_args.kwargs["headers"]["x-opencode-session"]

    assert first == second


def test_opencode_zen_by_base_url_sends_session_header():
    client = _make_client(provider="custom", base_url="https://opencode.ai/zen/v1")

    with patch("backend.llm_client.requests.post", return_value=_ok_response()) as post, \
         patch("backend.llm_client.log_api_call"):
        client.chat_completion([{"role": "user", "content": "hi"}])

    assert post.call_args.kwargs["headers"]["x-opencode-session"]


def test_non_opencode_provider_omits_session_header():
    client = _make_client(provider="openai", base_url="https://api.openai.com/v1")

    with patch("backend.llm_client.requests.post", return_value=_ok_response()) as post, \
         patch("backend.llm_client.log_api_call"):
        client.chat_completion([{"role": "user", "content": "hi"}])

    assert "x-opencode-session" not in post.call_args.kwargs["headers"]
