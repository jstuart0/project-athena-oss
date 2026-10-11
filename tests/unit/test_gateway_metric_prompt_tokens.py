"""The gateway's own metric writer carries the prompt-token count from Ollama's final chunk."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, "src")
sys.modules.setdefault("prometheus_client", mock.MagicMock())
os.environ.setdefault("SERVICE_API_KEY", "test-key-gateway-metric")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")

import gateway.main as gw  # noqa: E402
from shared.output_channel import OutputChannel  # noqa: E402


def _route_to_ollama(monkeypatch, final_chunk):
    class _Ollama:
        async def chat(self, **kwargs):
            yield {"done": True, "message": {"content": "Hello."}, **final_chunk}

    logged = mock.AsyncMock()

    monkeypatch.setattr(gw, "ollama_client", _Ollama())
    monkeypatch.setattr(gw, "_log_metric_to_db", logged)
    request = gw.ChatCompletionRequest(model="gpt-4", messages=[gw.ChatMessage(role="user", content="hi")])

    async def go():
        response = await gw.route_to_ollama(request, channel=OutputChannel.TEXT)
        await asyncio.sleep(0)                      # the metric write is a fire-and-forget task
        return response

    asyncio.run(go())
    logged.assert_awaited_once()
    return logged.await_args.kwargs


@pytest.mark.parametrize("final,expected", [
    ({"eval_count": 9, "prompt_eval_count": 321}, 321),
    ({"eval_count": 9, "prompt_eval_count": 0}, 0),
    ({"eval_count": 9}, None),
    ({"eval_count": 9, "prompt_eval_count": None}, None),
    ({"eval_count": 9, "prompt_eval_count": "321"}, None),
])
def test_route_to_ollama_logs_the_prompt_eval_count(monkeypatch, final, expected):
    kwargs = _route_to_ollama(monkeypatch, final)
    assert kwargs["prompt_tokens"] == expected
    assert kwargs["tokens"] == 9 and kwargs["source"] == "gateway", "gateway rows are tagged so they never read as orchestrator rows"


def test_only_the_direct_ollama_fallback_writes_a_gateway_row():
    """The orchestrator writes a row for every LLM call it makes (source=orchestrator). The gateway writes
    one only when it answers from Ollama itself, which the orchestrator never saw, so a proxied request
    produces no gateway row and no double count."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(gw.__file__).read_text(encoding="utf-8"))
    writers = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for call in ast.walk(fn):
                if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "_log_metric_to_db":
                    writers.add(fn.name)
    assert writers == {"route_to_ollama"}


def test_a_proxied_request_writes_no_gateway_row(monkeypatch):
    import httpx

    logged = mock.AsyncMock()
    monkeypatch.setattr(gw, "_log_metric_to_db", logged)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})),
        base_url="http://orchestrator.test",
    )
    monkeypatch.setattr(gw, "orchestrator_client", client)
    monkeypatch.setattr(gw, "orchestrator_circuit_breaker", None)
    monkeypatch.setattr(gw, "gateway_config", None)
    request = gw.ChatCompletionRequest(model="gpt-4", messages=[gw.ChatMessage(role="user", content="hi")])

    async def go():
        await gw.route_chat_completion_to_orchestrator(request, channel=OutputChannel.TEXT)
        await asyncio.sleep(0)

    asyncio.run(go())
    logged.assert_not_awaited()


def test_the_metric_writer_posts_prompt_tokens(monkeypatch):
    posted = []

    class _Client:
        async def post(self, url, json=None, headers=None):
            posted.append(json)
            return mock.MagicMock(status_code=201)

    monkeypatch.setattr(gw, "metric_client", _Client())

    async def go():
        await gw._log_metric_to_db(1.0, "m", "ollama", 0.5, 9, 18.0, prompt_tokens=321)
        await gw._log_metric_to_db(1.0, "m", "ollama", 0.5, 9, 18.0, prompt_tokens=0)
        await gw._log_metric_to_db(1.0, "m", "ollama", 0.5, 9, 18.0)

    asyncio.run(go())
    assert [p["prompt_tokens"] for p in posted] == [321, 0, None]
    assert all("prompt_tokens" in p for p in posted)
