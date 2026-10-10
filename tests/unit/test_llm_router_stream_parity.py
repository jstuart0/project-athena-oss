"""Unit tests for Ollama streaming/non-streaming request parity (ATHENA-87, F80).

`_generate_ollama` (non-streaming) and `_generate_ollama_stream` (streaming)
must build byte-identical `/api/generate` request bodies except for the
`stream` flag: same prompt composition (`/no_think` prefix), same qwen3
`think:false` rule, same options merge. `generate_stream` must also forward
`system_prompt` to every backend branch (Ollama, OpenAI, Anthropic, Google),
matching what the non-streaming `generate()` already does.

Transport harness: real httpx request-building (JSON serialization, headers,
the real NDJSON line-parser), faked only at the socket via
`httpx.MockTransport`. `real_async_client` is bound to `httpx.AsyncClient`
BEFORE any monkeypatch runs, so the factory's own `httpx.AsyncClient(...)`
call inside `shared.llm_router` doesn't recurse into itself once patched.
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx
import pytest

sys.path.insert(0, "src")

import shared.llm_router as llm_router_module  # noqa: E402
from shared.llm_router import LLMRouter, BackendType  # noqa: E402

# Bound before any test patches shared.llm_router.httpx.AsyncClient.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _run(coro):
    return asyncio.run(coro)


def _client_factory(handler):
    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(*args, **kwargs, transport=httpx.MockTransport(handler))
    return factory


def _nonstream_response(text: str = "ok") -> httpx.Response:
    return httpx.Response(200, json={"response": text, "done": True})


_STREAM_BODY = (
    '{"response":"Hel"}\n'
    '{"response":"lo"}\n'
    '{"response":"","done":true,"eval_count":2}\n'
)


def _stream_response() -> httpx.Response:
    return httpx.Response(200, text=_STREAM_BODY)


async def _consume_stream(gen):
    chunks = []
    async for chunk in gen:
        chunks.append(chunk)
    return chunks


def _make_router() -> LLMRouter:
    # These tests are about the request payload; metric rows are covered in test_llm_prompt_tokens.py.
    return LLMRouter(admin_url="http://admin.test", persist_metrics=False)


OLLAMA_OPTIONS = {"num_ctx": 8192, "top_k": None}


@pytest.mark.parametrize(
    "model",
    ["qwen3:4b-instruct-2507-q4_K_M", "Qwen3:8B", "llama3.1:8b"],
    ids=["qwen3_instruct", "qwen3_capital_q", "llama3_1"],
)
@pytest.mark.parametrize(
    "system_prompt",
    ["/no_think", None],
    ids=["no_think", "none"],
)
def test_stream_and_nonstream_ollama_payloads_identical_except_stream(monkeypatch, model, system_prompt):
    captured = {}

    def handler(request):
        body = json.loads(request.content)
        if body.get("stream"):
            captured["stream_body"] = body
            return _stream_response()
        captured["nonstream_body"] = body
        return _nonstream_response()

    monkeypatch.setattr(llm_router_module.httpx, "AsyncClient", _client_factory(handler))
    router = _make_router()

    async def scenario():
        await router._generate_ollama(
            endpoint_url="http://ollama.test",
            model=model,
            prompt="Q",
            temperature=0.7,
            max_tokens=100,
            timeout=30,
            keep_alive=-1,
            ollama_options=OLLAMA_OPTIONS,
            system_prompt=system_prompt,
        )
        await _consume_stream(router._generate_ollama_stream(
            endpoint_url="http://ollama.test",
            model=model,
            prompt="Q",
            temperature=0.7,
            max_tokens=100,
            timeout=30,
            keep_alive=-1,
            ollama_options=OLLAMA_OPTIONS,
            system_prompt=system_prompt,
        ))

    _run(scenario())

    nonstream_body = captured["nonstream_body"]
    stream_body = captured["stream_body"]

    assert nonstream_body["stream"] is False
    assert stream_body["stream"] is True
    assert {k: v for k, v in nonstream_body.items() if k != "stream"} == {
        k: v for k, v in stream_body.items() if k != "stream"
    }


def test_stream_payload_disables_thinking_for_qwen3_only(monkeypatch):
    captured = {}

    def handler(request):
        body = json.loads(request.content)
        captured[body["model"]] = body
        return _stream_response() if body.get("stream") else _nonstream_response()

    monkeypatch.setattr(llm_router_module.httpx, "AsyncClient", _client_factory(handler))
    router = _make_router()

    async def scenario():
        await _consume_stream(router._generate_ollama_stream(
            endpoint_url="http://ollama.test",
            model="qwen3:4b-instruct-2507-q4_K_M",
            prompt="Q",
            temperature=0.7,
            max_tokens=100,
            timeout=30,
        ))
        await _consume_stream(router._generate_ollama_stream(
            endpoint_url="http://ollama.test",
            model="llama3.1:8b",
            prompt="Q",
            temperature=0.7,
            max_tokens=100,
            timeout=30,
        ))

    _run(scenario())

    assert captured["qwen3:4b-instruct-2507-q4_K_M"]["think"] is False
    assert "think" not in captured["llama3.1:8b"]


def test_stream_payload_prefixes_no_think_marker(monkeypatch):
    captured = {}

    def handler(request):
        body = json.loads(request.content)
        captured["body"] = body
        return _stream_response()

    monkeypatch.setattr(llm_router_module.httpx, "AsyncClient", _client_factory(handler))
    router = _make_router()

    _run(_consume_stream(router._generate_ollama_stream(
        endpoint_url="http://ollama.test",
        model="qwen3:4b",
        prompt="Q",
        temperature=0.7,
        max_tokens=100,
        timeout=30,
        system_prompt="/no_think",
    )))

    assert captured["body"]["prompt"] == "/no_think\n\nQ"


def test_generate_stream_forwards_system_prompt_to_ollama(monkeypatch):
    captured = {}

    def handler(request):
        body = json.loads(request.content)
        captured["body"] = body
        return _stream_response()

    monkeypatch.setattr(llm_router_module.httpx, "AsyncClient", _client_factory(handler))
    router = _make_router()

    async def fake_backend_config(model):
        return {"endpoint_url": "http://ollama.test", "backend_type": BackendType.OLLAMA}

    async def fake_model_config(model):
        return {}

    monkeypatch.setattr(router, "_get_backend_config", fake_backend_config)
    monkeypatch.setattr(router, "_get_model_config", fake_model_config)

    _run(_consume_stream(router.generate_stream(model="qwen3:4b", prompt="Q", system_prompt="/no_think")))

    assert captured["body"]["prompt"].startswith("/no_think\n\n")


@pytest.mark.parametrize("backend_type", [BackendType.OPENAI, BackendType.ANTHROPIC, BackendType.GOOGLE], ids=["openai", "anthropic", "google"])
def test_generate_stream_forwards_system_prompt_to_cloud(monkeypatch, backend_type):
    router = _make_router()

    async def fake_backend_config(model):
        return {"endpoint_url": None, "backend_type": backend_type, "model_id": "test-model"}

    async def fake_model_config(model):
        return {}

    async def fake_cloud_credentials(provider):
        return {"api_key": "k"}

    monkeypatch.setattr(router, "_get_backend_config", fake_backend_config)
    monkeypatch.setattr(router, "_get_model_config", fake_model_config)
    monkeypatch.setattr(router, "_get_cloud_credentials", fake_cloud_credentials)

    captured_kwargs = {}

    if backend_type in (BackendType.OPENAI, BackendType.ANTHROPIC):
        async def fake_stream(**kwargs):
            captured_kwargs.update(kwargs)
            yield {"token": "x", "done": True}

        attr = "_generate_openai_stream" if backend_type == BackendType.OPENAI else "_generate_anthropic_stream"
        monkeypatch.setattr(router, attr, fake_stream)

        _run(_consume_stream(router.generate_stream(model="cloud-model", prompt="Q", system_prompt="/no_think")))

        assert captured_kwargs.get("system_prompt") == "/no_think"
    else:
        from unittest.mock import AsyncMock

        spy = AsyncMock(return_value={"response": "x"})
        monkeypatch.setattr(router, "_generate_google", spy)

        _run(_consume_stream(router.generate_stream(model="cloud-model", prompt="Q", system_prompt="/no_think")))

        assert spy.await_args.kwargs.get("system_prompt") == "/no_think"


def test_stream_still_yields_tokens_and_final_stats(monkeypatch):
    def handler(request):
        return _stream_response()

    monkeypatch.setattr(llm_router_module.httpx, "AsyncClient", _client_factory(handler))
    router = _make_router()

    chunks = _run(_consume_stream(router._generate_ollama_stream(
        endpoint_url="http://ollama.test",
        model="llama3.1:8b",
        prompt="Q",
        temperature=0.7,
        max_tokens=100,
        timeout=30,
    )))

    tokens = [c["token"] for c in chunks if c["token"]]
    assert tokens == ["Hel", "lo"]

    final = chunks[-1]
    assert final["done"] is True
    assert final["eval_count"] == 2
