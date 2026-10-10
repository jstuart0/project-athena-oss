"""The gateway's non-streaming orchestrator path passes the orchestrator's real `usage`
through unchanged, and falls back to a word-count estimate only when it is absent."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest import mock

import httpx
import pytest

sys.path.insert(0, "src")
sys.modules.setdefault("prometheus_client", mock.MagicMock())
os.environ.setdefault("SERVICE_API_KEY", "test-key-gateway-usage")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")

import gateway.main as gw  # noqa: E402
from shared.output_channel import OutputChannel  # noqa: E402

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "openai_chat_response_v1.json"


def _route(monkeypatch, orchestrator_body, user_text="what is the capital of France"):
    def handler(request):
        return httpx.Response(200, json=orchestrator_body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://orchestrator.test")
    monkeypatch.setattr(gw, "orchestrator_client", client)
    monkeypatch.setattr(gw, "orchestrator_circuit_breaker", None)
    monkeypatch.setattr(gw, "gateway_config", None)
    request = gw.ChatCompletionRequest(model="gpt-4", messages=[gw.ChatMessage(role="user", content=user_text)])
    return asyncio.run(gw.route_chat_completion_to_orchestrator(request, channel=OutputChannel.TEXT))


def test_the_orchestrators_golden_usage_passes_through_unchanged(monkeypatch):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    response = _route(monkeypatch, golden)
    assert response.usage == golden["usage"] == {"prompt_tokens": 321, "completion_tokens": 12, "total_tokens": 333}
    assert response.choices[0].message.content == "The answer."


def test_real_zeros_pass_through_too(monkeypatch):
    body = {"choices": [{"message": {"content": "Hello. How can I help?"}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
    assert _route(monkeypatch, body).usage == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


@pytest.mark.parametrize("usage", [
    None, "none", {}, {"prompt_tokens": 5}, {"prompt_tokens": "5", "completion_tokens": 1, "total_tokens": 6},
    {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2}, {"prompt_tokens": 1.5, "completion_tokens": 1, "total_tokens": 2},
])
def test_absent_or_malformed_usage_falls_back_to_the_word_count_estimate(monkeypatch, usage):
    body = {"choices": [{"message": {"content": "one two three"}}]}
    if usage is not None:
        body["usage"] = usage
    assert _route(monkeypatch, body, "a b").usage == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}


def test_extra_keys_in_the_orchestrators_usage_are_not_forwarded(monkeypatch):
    body = {"choices": [{"message": {"content": "x"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "secret": "k"}}
    assert _route(monkeypatch, body).usage == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}


# --- stream_options.include_usage through the gateway ------------------------------------------------------------------


def _payload(stream_options, *, stream=True):
    request = gw.ChatCompletionRequest(
        model="gpt-4", messages=[gw.ChatMessage(role="user", content="hi")], stream=stream, stream_options=stream_options)
    return gw._orchestrator_openai_payload(request, "kitchen", stream=stream, channel=OutputChannel.TEXT)


def test_include_usage_is_forwarded_to_the_orchestrator_on_a_stream():
    assert _payload({"include_usage": True})["stream_options"] == {"include_usage": True}


@pytest.mark.parametrize("options", [None, {}, {"include_usage": False}, {"include_usage": "true"}, {"other": 1}])
def test_nothing_else_is_forwarded(options):
    assert "stream_options" not in _payload(options)


def test_a_non_stream_request_does_not_forward_stream_options():
    assert "stream_options" not in _payload({"include_usage": True}, stream=False)


def test_the_gateway_passes_the_usage_chunk_through_untouched(monkeypatch):
    usage_chunk = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m", "choices": [],
                   "usage": {"prompt_tokens": 321, "completion_tokens": 12, "total_tokens": 333}}
    lines = [
        "data: " + json.dumps({"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "m",
                               "choices": [{"index": 0, "delta": {"content": "Hi"}, "finish_reason": None}]}),
        "data: " + json.dumps(usage_chunk),
        "data: [DONE]",
    ]

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for line in lines:
                yield line

    seen = {}

    class _Client:
        def stream(self, method, path, json=None, timeout=None):
            seen["payload"] = json
            return _Stream()

    monkeypatch.setattr(gw, "orchestrator_client", _Client())
    request = gw.ChatCompletionRequest(
        model="gpt-4", messages=[gw.ChatMessage(role="user", content="hi")], stream=True, stream_options={"include_usage": True})

    async def go():
        return [line async for line in gw.stream_orchestrator_response(request, "kitchen", channel=OutputChannel.TEXT)]

    out = asyncio.run(go())
    assert seen["payload"]["stream_options"] == {"include_usage": True}
    assert [line.strip() for line in out] == lines
