"""Each backend parser reports the prompt-token count the provider gave it.

All 13 `_generate_*` functions of `LLMRouter` run for real against response
bodies in `tests/fixtures/llm_backends/` (shapes from each provider's documented
response format). Ollama and MLX go over `httpx.MockTransport`. The OpenAI,
Anthropic and Google functions build their client from the provider SDK, which
cannot be pointed at a mock transport, so the same JSON bodies are turned into
SDK-shaped objects and served by a fake SDK module. (Named waiver: real
providers' byte-level wire formats are covered by the SDKs' own test suites.)

The rule under test: a reported count is kept (0 stays 0), a missing or null
usage is None, and Anthropic counts input plus cache reads plus cache writes.
The function list is read from the class, so a new `_generate_*` fails here
until it has a driver.
"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

import httpx
import pytest

sys.path.insert(0, "src")

from shared import llm_router  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "llm_backends"
FUNCTION_NAMES = sorted(n for n in dir(llm_router.LLMRouter) if n.startswith("_generate_"))


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def ns(value, key=None):
    """JSON -> SDK-shaped objects (attribute access). A Google `finish_reason` is an enum with `.name`."""
    if isinstance(value, dict):
        return SimpleNamespace(**{k: ns(v, k) for k, v in value.items()})
    if isinstance(value, list):
        return [ns(v) for v in value]
    if key == "finish_reason" and isinstance(value, str):
        return SimpleNamespace(name=value)
    return value


def _router():
    return llm_router.LLMRouter(admin_url="http://admin.test", persist_metrics=False)


def _http(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(llm_router.httpx, "AsyncClient", factory)


def _collect(agen):
    async def run():
        return [c async for c in agen]

    return asyncio.run(run())


# --- bodies with the prompt count set to present / zero / absent ------------------------------------

PRESENT, ZERO, ABSENT = "present", "zero", "absent"


def _ollama(body, mode):
    body = copy.deepcopy(body)
    if mode == ZERO:
        body["prompt_eval_count"] = 0
    elif mode == ABSENT:
        body.pop("prompt_eval_count")
    return body


def _usage_object(body, mode, field="usage"):
    body = copy.deepcopy(body)
    if mode == ZERO:
        for key in body[field]:
            if key.startswith(("prompt", "input")):
                body[field][key] = 0
        for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            if key in body[field]:
                body[field][key] = 0
    elif mode == ABSENT:
        body[field] = None
    return body


# --- drivers: name -> (mode -> final result / final chunk) ----------------------------------------------------


def _drive_ollama_generate(monkeypatch, mode):
    body = _ollama(load("ollama_generate.json"), mode)
    _http(monkeypatch, lambda request: httpx.Response(200, json=body))
    return asyncio.run(_router()._generate_ollama("http://o.test", "m", "p", 0.1, 100, 5))


def _drive_ollama_tools(monkeypatch, mode):
    body = _ollama(load("ollama_tools.json"), mode)
    _http(monkeypatch, lambda request: httpx.Response(200, json=body))
    return asyncio.run(_router()._generate_ollama_with_tools("m", [{"role": "user", "content": "x"}], None, 0.1, 100, None, timeout=5))


def _drive_ollama_stream(monkeypatch, mode):
    lines = load("ollama_stream.json")
    lines[-1] = _ollama(lines[-1], mode)
    _http(monkeypatch, lambda request: httpx.Response(200, text="\n".join(json.dumps(x) for x in lines)))
    return _collect(_router()._generate_ollama_stream("http://o.test", "m", "p", 0.1, 100, 5))[-1]


def _drive_mlx_generate(monkeypatch, mode):
    body = _usage_object(load("mlx_generate.json"), mode)
    _http(monkeypatch, lambda request: httpx.Response(200, json=body))
    return asyncio.run(_router()._generate_mlx("http://m.test", "m", "p", 0.1, 100, 5))


def _drive_mlx_stream(monkeypatch, mode):
    chunks = load("mlx_stream.json")
    if mode == ZERO:
        chunks[-1]["usage"]["prompt_tokens"] = 0
    elif mode == ABSENT:
        chunks[-1].pop("usage")
    sse = "\n".join(["data: " + json.dumps(c) for c in chunks] + ["data: [DONE]"])
    _http(monkeypatch, lambda request: httpx.Response(200, text=sse))
    return _collect(_router()._generate_mlx_stream("http://m.test", "m", "p", 0.1, 100, 5))[-1]


def _install_openai(monkeypatch, body, streaming=False):
    """The OpenAI SDK is in the test lock, so responses are built with its own pydantic models
    (ChatCompletion / ChatCompletionChunk): a drift in the SDK's shape fails here, not in production."""
    from openai.types.chat import ChatCompletion, ChatCompletionChunk

    async def create(**kwargs):
        if kwargs.get("stream"):
            async def stream():
                for chunk in body:
                    yield ChatCompletionChunk.model_validate(chunk)

            return stream()
        return ChatCompletion.model_validate(body)

    module = ModuleType("openai")
    module.AsyncOpenAI = lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setitem(sys.modules, "openai", module)


def _drive_openai_generate(monkeypatch, mode):
    _install_openai(monkeypatch, _usage_object(load("openai_chat.json"), mode))
    return asyncio.run(_router()._generate_openai("k", "m", "p", 0.1, 100))


def _drive_openai_tools(monkeypatch, mode):
    _install_openai(monkeypatch, _usage_object(load("openai_chat.json"), mode))
    return asyncio.run(_router()._generate_openai_with_tools("m", [{"role": "user", "content": "x"}], None, 0.1, 100, None, api_key="k"))


def _drive_openai_stream(monkeypatch, mode):
    chunks = load("openai_stream.json")
    if mode == ZERO:
        chunks[-1]["usage"]["prompt_tokens"] = 0
    elif mode == ABSENT:
        chunks[-1]["usage"] = None
    _install_openai(monkeypatch, chunks)
    router = _router()
    monkeypatch.setattr(router, "_get_model_pricing", mock.AsyncMock(return_value={}))
    monkeypatch.setattr(router, "_calculate_cloud_cost", lambda *a, **k: 0.0)
    monkeypatch.setattr(router, "_track_cloud_usage", mock.AsyncMock())
    return _collect(router._generate_openai_stream("k", "m", "p", 0.1, 100))[-1]


def _install_anthropic(monkeypatch, message=None, events=None):
    async def create(**kwargs):
        return ns(message)

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def __aiter__(self):
            async def gen():
                for event in events:
                    yield ns(event)

            return gen()

    module = ModuleType("anthropic")
    module.AsyncAnthropic = lambda **kw: SimpleNamespace(messages=SimpleNamespace(create=create, stream=lambda **k: _Stream()))
    monkeypatch.setitem(sys.modules, "anthropic", module)


def _anthropic_message(mode):
    body = _usage_object(load("anthropic_message.json"), mode)
    return body


def _drive_anthropic_generate(monkeypatch, mode):
    _install_anthropic(monkeypatch, message=_anthropic_message(mode))
    return asyncio.run(_router()._generate_anthropic("k", "m", "p", 0.1, 100))


def _drive_anthropic_tools(monkeypatch, mode):
    _install_anthropic(monkeypatch, message=_anthropic_message(mode))
    return asyncio.run(_router()._generate_anthropic_with_tools("k", "m", [{"role": "user", "content": "x"}], [], 0.1, 100, None))


def _drive_anthropic_stream(monkeypatch, mode):
    events = load("anthropic_stream.json")
    if mode == ZERO:
        for key in events[0]["message"]["usage"]:
            events[0]["message"]["usage"][key] = 0
    elif mode == ABSENT:
        events = events[1:]
    _install_anthropic(monkeypatch, events=events)
    router = _router()
    monkeypatch.setattr(router, "_get_model_pricing", mock.AsyncMock(return_value={}))
    monkeypatch.setattr(router, "_calculate_cloud_cost", lambda *a, **k: 0.0)
    monkeypatch.setattr(router, "_track_cloud_usage", mock.AsyncMock())
    return _collect(router._generate_anthropic_stream("k", "m", "p", 0.1, 100))[-1]


def _install_google(monkeypatch, body):
    class _Model:
        def __init__(self, *args, **kwargs):
            pass

        async def generate_content_async(self, *args, **kwargs):
            return ns(body)

    genai = ModuleType("google.generativeai")
    genai.configure = lambda api_key=None: None
    genai.GenerativeModel = _Model
    genai.types = SimpleNamespace(GenerationConfig=lambda **kw: kw)
    package = ModuleType("google")
    package.generativeai = genai
    monkeypatch.setitem(sys.modules, "google", package)
    monkeypatch.setitem(sys.modules, "google.generativeai", genai)


def _google_body(mode):
    body = load("google_response.json")
    if mode == ZERO:
        body["usage_metadata"]["prompt_token_count"] = 0
    elif mode == ABSENT:
        body["usage_metadata"] = None
    return body


def _drive_google_generate(monkeypatch, mode):
    _install_google(monkeypatch, _google_body(mode))
    return asyncio.run(_router()._generate_google("k", "m", "p", 0.1, 100))


def _drive_google_tools(monkeypatch, mode):
    _install_google(monkeypatch, _google_body(mode))
    return asyncio.run(_router()._generate_google_with_tools("k", "m", [{"role": "user", "content": "x"}], [], 0.1, 100, None))


DRIVERS = {
    "_generate_ollama": (_drive_ollama_generate, 321),
    "_generate_ollama_with_tools": (_drive_ollama_tools, 321),
    "_generate_ollama_stream": (_drive_ollama_stream, 321),
    "_generate_mlx": (_drive_mlx_generate, 321),
    "_generate_mlx_stream": (_drive_mlx_stream, 321),
    "_generate_openai": (_drive_openai_generate, 321),
    "_generate_openai_with_tools": (_drive_openai_tools, 321),
    "_generate_openai_stream": (_drive_openai_stream, 321),
    "_generate_anthropic": (_drive_anthropic_generate, 3120),
    "_generate_anthropic_with_tools": (_drive_anthropic_tools, 3120),
    "_generate_anthropic_stream": (_drive_anthropic_stream, 3120),
    "_generate_google": (_drive_google_generate, 321),
    "_generate_google_with_tools": (_drive_google_tools, 321),
}


def test_every_generate_function_has_a_driver():
    assert len(FUNCTION_NAMES) >= 13
    assert "_generate_ollama" in FUNCTION_NAMES
    assert set(FUNCTION_NAMES) == set(DRIVERS), "a _generate_* function was added or removed without updating the drivers"


@pytest.mark.parametrize("name", FUNCTION_NAMES)
def test_a_reported_count_is_kept(monkeypatch, name):
    driver, expected = DRIVERS[name]
    assert driver(monkeypatch, PRESENT)["prompt_eval_count"] == expected


@pytest.mark.parametrize("name", FUNCTION_NAMES)
def test_a_reported_zero_stays_zero(monkeypatch, name):
    driver, _ = DRIVERS[name]
    result = driver(monkeypatch, ZERO)
    assert result["prompt_eval_count"] == 0 and result["prompt_eval_count"] is not None


@pytest.mark.parametrize("name", FUNCTION_NAMES)
def test_a_missing_count_is_none_not_zero(monkeypatch, name):
    driver, _ = DRIVERS[name]
    assert driver(monkeypatch, ABSENT)["prompt_eval_count"] is None


def test_anthropic_counts_cache_reads_and_writes_but_cost_keeps_input_tokens(monkeypatch):
    message = load("anthropic_message.json")
    message["usage"].update(input_tokens=120, cache_read_input_tokens=3000, cache_creation_input_tokens=7)
    _install_anthropic(monkeypatch, message=message)
    result = asyncio.run(_router()._generate_anthropic("k", "m", "p", 0.1, 100))
    assert result["prompt_eval_count"] == 3127
    assert result["input_tokens"] == 120, "cloud cost is still computed from input_tokens alone"


def test_anthropic_missing_cache_fields_count_zero(monkeypatch):
    message = load("anthropic_message.json")
    message["usage"] = {"input_tokens": 120, "output_tokens": 12}
    _install_anthropic(monkeypatch, message=message)
    assert asyncio.run(_router()._generate_anthropic("k", "m", "p", 0.1, 100))["prompt_eval_count"] == 120


def test_anthropic_null_cache_fields_count_zero(monkeypatch):
    message = load("anthropic_message.json")
    message["usage"].update(cache_read_input_tokens=None, cache_creation_input_tokens=None)
    _install_anthropic(monkeypatch, message=message)
    assert asyncio.run(_router()._generate_anthropic("k", "m", "p", 0.1, 100))["prompt_eval_count"] == 120


def test_the_mlx_tool_branch_reports_usage(monkeypatch):
    body = load("mlx_generate.json")
    _http(monkeypatch, lambda request: httpx.Response(200, json=body))
    router = _router()
    monkeypatch.setattr(router, "_get_backend_config", mock.AsyncMock(return_value={"backend_type": "mlx", "endpoint_url": "http://m.test"}))
    result = asyncio.run(router.generate_with_tools(model="m", messages=[{"role": "user", "content": "x"}], tools=[], backend="mlx"))
    assert result["prompt_eval_count"] == 321
    body["usage"] = None
    assert asyncio.run(router.generate_with_tools(model="m", messages=[{"role": "user", "content": "x"}], tools=[], backend="mlx"))["prompt_eval_count"] is None
