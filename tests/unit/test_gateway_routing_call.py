"""The gateway makes no LLM call ahead of the orchestrator on the OpenAI-shaped routes.

`/v1/chat/completions` used to await `is_athena_query`, which calls the
classifier model when `llm_based_routing` is on, for a log-only result. It now
reads the keyword match and goes straight to the orchestrator.
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, "src")

sys.modules.setdefault("prometheus_client", mock.MagicMock())
os.environ.setdefault("SERVICE_API_KEY", "test-key-gateway-routing-call")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")

import gateway.main as gw  # noqa: E402
from gateway.conversation_limiter import NewConversationLimiter  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class _Orchestrator:
    def __init__(self):
        self.payloads = []

    async def post(self, path, json=None, **kwargs):
        self.payloads.append(json)
        response = mock.MagicMock()
        response.raise_for_status = mock.MagicMock()
        response.json.return_value = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        return response

    def stream(self, method, path, json=None, timeout=None):
        self.payloads.append(json)

        class _Ctx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def raise_for_status(self):
                return None

            async def aiter_lines(self):
                yield "data: [DONE]"

        return _Ctx()


@pytest.fixture
def orchestrator(monkeypatch):
    fake = _Orchestrator()
    monkeypatch.setattr(gw, "orchestrator_client", fake)
    monkeypatch.setattr(gw, "orchestrator_timeout", 5)
    monkeypatch.setattr(gw, "orchestrator_circuit_breaker", None)
    monkeypatch.setattr(gw, "gateway_config", None)
    monkeypatch.setattr(gw, "global_rate_limiter", None)
    monkeypatch.setattr(gw, "new_conversation_limiter", NewConversationLimiter(per_minute=100000))
    monkeypatch.setattr(gw, "_detect_room_from_active_satellite", mock.AsyncMock(return_value="kitchen"))
    monkeypatch.setattr(gw, "_SPEECH_CLIENT_NETWORKS", ())
    monkeypatch.setattr(gw, "_TRUSTED_PROXY_NETWORKS", ())
    return fake


@pytest.fixture
def llm_routing_on(monkeypatch):
    """`llm_based_routing` enabled, and a classifier that fails the test if awaited."""
    classifier = mock.AsyncMock(side_effect=AssertionError("classify_intent_llm awaited"))
    monkeypatch.setattr(gw, "classify_intent_llm", classifier)
    monkeypatch.setattr(gw, "is_feature_enabled", mock.AsyncMock(return_value=True))
    return classifier


@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
@pytest.mark.parametrize("text", ["what's the weather", "hello", "tell me a story"])
def test_chat_completions_never_awaits_the_routing_llm(orchestrator, llm_routing_on, stream, text):
    client = TestClient(gw.app)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "m", "stream": stream, "messages": [{"role": "user", "content": text}]},
    )
    assert resp.status_code == 200
    llm_routing_on.assert_not_awaited()
    assert len(orchestrator.payloads) == 1, "the request still reaches the orchestrator"


def test_is_athena_query_would_have_called_the_classifier(llm_routing_on):
    """Positive control: with the flag on, the old call does await the classifier."""
    import asyncio

    with pytest.raises(AssertionError, match="classify_intent_llm awaited"):
        asyncio.run(gw.is_athena_query([gw.ChatMessage(role="user", content="hello")]))
    llm_routing_on.assert_awaited_once()


def test_gateway_source_has_no_remaining_is_athena_query_await():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(gw.__file__)).read_text(encoding="utf-8"))
    awaited = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)
        and getattr(n.value.func, "id", None) in ("is_athena_query", "classify_intent_llm")
    ]
    inside = {"is_athena_query"}  # the function itself still awaits the classifier; nothing else may call it
    callers = [
        (fn.name, call.lineno)
        for fn in ast.walk(tree) if isinstance(fn, ast.AsyncFunctionDef) and fn.name not in inside and fn.name != "classify_intent_llm"
        for call in ast.walk(fn)
        if isinstance(call, ast.Await) and isinstance(call.value, ast.Call)
        and getattr(call.value.func, "id", None) in ("is_athena_query", "classify_intent_llm")
    ]
    assert awaited, "population: the helpers are still defined and used inside is_athena_query"
    assert callers == []
