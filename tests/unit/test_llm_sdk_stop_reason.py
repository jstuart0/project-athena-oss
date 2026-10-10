"""The cloud backends' real parsers report a cut-off answer as `stop_reason == "length"`.

The provider SDKs aren't installed in the unit environment, so each test
installs a fake SDK module with the real response shape (OpenAI `choices[0]
.finish_reason`, Anthropic `stop_reason` and stream `message_delta`, Google
`candidates[0].finish_reason` as an enum with `.name`) and runs the router's
own `_generate_*` code against it. Every generate and stream path is covered
for `length` and for a natural stop.
"""
from __future__ import annotations

import asyncio
import enum
import sys
from types import ModuleType, SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, "src")

from shared import llm_router  # noqa: E402
from shared.output_channel import answer_hit_cap  # noqa: E402

CAP = 200


def _router(monkeypatch, backend):
    router = llm_router.LLMRouter(admin_url="http://admin.test", persist_metrics=False)
    monkeypatch.setattr(router, "_get_backend_config", mock.AsyncMock(
        return_value={"endpoint_url": None, "backend_type": backend, "model_id": "m"}))
    monkeypatch.setattr(router, "_get_model_config", mock.AsyncMock(return_value={}))
    monkeypatch.setattr(router, "_get_cloud_credentials", mock.AsyncMock(return_value={"api_key": "k"}))
    monkeypatch.setattr(router, "_get_model_pricing", mock.AsyncMock(return_value={}))
    monkeypatch.setattr(router, "_calculate_cloud_cost", lambda *a, **k: 0.0)
    monkeypatch.setattr(router, "_track_cloud_usage", mock.AsyncMock())
    return router


def _collect(agen):
    async def run():
        return [chunk async for chunk in agen]

    return asyncio.run(run())


def _kwargs():
    return dict(api_key="k", model="m", prompt="p", temperature=0.1, max_tokens=CAP)


# --- Google ---------------------------------------------------------------------------------------


class FinishReason(enum.Enum):
    STOP = 1
    MAX_TOKENS = 2
    SAFETY = 3


def _install_google(monkeypatch, candidates):
    class _Model:
        def __init__(self, model_name=None, system_instruction=None):
            pass

        async def generate_content_async(self, prompt, generation_config=None):
            return SimpleNamespace(
                text="Hello there.",
                usage_metadata=SimpleNamespace(prompt_token_count=5, candidates_token_count=CAP),
                candidates=candidates,
            )

    genai = ModuleType("google.generativeai")
    genai.configure = lambda api_key=None: None
    genai.GenerativeModel = _Model
    genai.types = SimpleNamespace(GenerationConfig=lambda **kw: kw)
    package = ModuleType("google")
    package.generativeai = genai
    monkeypatch.setitem(sys.modules, "google", package)
    monkeypatch.setitem(sys.modules, "google.generativeai", genai)


@pytest.mark.parametrize("reason,stop,hit", [
    (FinishReason.MAX_TOKENS, "length", True),
    (FinishReason.STOP, "stop", False),
    (FinishReason.SAFETY, None, True),          # no usable reason: falls to the token count (200 >= cap-5)
])
def test_google_generate_reads_the_candidate_finish_reason(monkeypatch, reason, stop, hit):
    _install_google(monkeypatch, [SimpleNamespace(finish_reason=reason)])
    result = asyncio.run(_router(monkeypatch, llm_router.BackendType.GOOGLE)._generate_google(**_kwargs()))
    assert result["stop_reason"] == stop
    assert result["finish_reason"] == reason.name
    assert answer_hit_cap({**result, "eval_count": CAP}, CAP) is hit


def test_google_generate_with_no_candidates_has_no_reason(monkeypatch):
    _install_google(monkeypatch, [])
    result = asyncio.run(_router(monkeypatch, llm_router.BackendType.GOOGLE)._generate_google(**_kwargs()))
    assert result["stop_reason"] is None and result["finish_reason"] == "unknown"


@pytest.mark.parametrize("reason,stop", [(FinishReason.MAX_TOKENS, "length"), (FinishReason.STOP, "stop")])
def test_google_stream_final_chunk_carries_the_stop_reason(monkeypatch, reason, stop):
    _install_google(monkeypatch, [SimpleNamespace(finish_reason=reason)])
    router = _router(monkeypatch, llm_router.BackendType.GOOGLE)
    chunks = _collect(router.generate_stream(model="m", prompt="p", max_tokens=CAP))
    assert chunks[-1]["done"] is True and chunks[-1]["stop_reason"] == stop


def test_google_no_longer_hardcodes_stop():
    import inspect

    source = inspect.getsource(llm_router.LLMRouter._generate_google)
    assert '"stop_reason": "stop"' not in source and '"finish_reason": "stop"' not in source


# --- OpenAI ----------------------------------------------------------------------------------------


def _install_openai(monkeypatch, finish_reason):
    async def create(**kwargs):
        if kwargs.get("stream"):
            async def stream():
                yield SimpleNamespace(usage=None, choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="Hello"), finish_reason=None)])
                yield SimpleNamespace(usage=None, choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=None), finish_reason=finish_reason)])
                yield SimpleNamespace(usage=SimpleNamespace(prompt_tokens=5, completion_tokens=CAP), choices=[])

            return stream()
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content="Hello there."))],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=CAP),
        )

    module = ModuleType("openai")
    module.AsyncOpenAI = lambda api_key=None: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setitem(sys.modules, "openai", module)


@pytest.mark.parametrize("finish,stop", [("length", "length"), ("stop", "stop")])
def test_openai_generate_carries_the_stop_reason(monkeypatch, finish, stop):
    _install_openai(monkeypatch, finish)
    result = asyncio.run(_router(monkeypatch, llm_router.BackendType.OPENAI)._generate_openai(**_kwargs()))
    assert result["stop_reason"] == stop
    assert answer_hit_cap(result, CAP) is (stop == "length")


@pytest.mark.parametrize("finish,stop", [("length", "length"), ("stop", "stop")])
def test_openai_stream_final_chunk_carries_the_stop_reason(monkeypatch, finish, stop):
    _install_openai(monkeypatch, finish)
    router = _router(monkeypatch, llm_router.BackendType.OPENAI)
    chunks = _collect(router.generate_stream(model="m", prompt="p", max_tokens=CAP))
    assert chunks[-1]["done"] is True and chunks[-1]["stop_reason"] == stop
    assert answer_hit_cap(chunks[-1], CAP) is (stop == "length")


# --- Anthropic ----------------------------------------------------------------------------------------


def _install_anthropic(monkeypatch, stop_reason):
    async def create(**kwargs):
        return SimpleNamespace(
            content=[SimpleNamespace(text="Hello there.")],
            usage=SimpleNamespace(input_tokens=5, output_tokens=CAP),
            stop_reason=stop_reason,
        )

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def __aiter__(self):
            async def events():
                yield SimpleNamespace(type="message_start", message=SimpleNamespace(usage=SimpleNamespace(input_tokens=5)))
                yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(text="Hello"))
                yield SimpleNamespace(type="message_delta", delta=SimpleNamespace(stop_reason=stop_reason),
                                      usage=SimpleNamespace(output_tokens=CAP))

            return events()

    module = ModuleType("anthropic")
    module.AsyncAnthropic = lambda api_key=None: SimpleNamespace(
        messages=SimpleNamespace(create=create, stream=lambda **kw: _Stream()))
    monkeypatch.setitem(sys.modules, "anthropic", module)


@pytest.mark.parametrize("raw,stop", [("max_tokens", "length"), ("end_turn", "stop")])
def test_anthropic_generate_carries_the_stop_reason(monkeypatch, raw, stop):
    _install_anthropic(monkeypatch, raw)
    result = asyncio.run(_router(monkeypatch, llm_router.BackendType.ANTHROPIC)._generate_anthropic(**_kwargs()))
    assert result["stop_reason"] == stop
    assert answer_hit_cap(result, CAP) is (stop == "length")


@pytest.mark.parametrize("raw,stop", [("max_tokens", "length"), ("end_turn", "stop")])
def test_anthropic_stream_final_chunk_carries_the_stop_reason(monkeypatch, raw, stop):
    _install_anthropic(monkeypatch, raw)
    router = _router(monkeypatch, llm_router.BackendType.ANTHROPIC)
    chunks = _collect(router.generate_stream(model="m", prompt="p", max_tokens=CAP))
    assert chunks[-1]["done"] is True and chunks[-1]["stop_reason"] == stop
    assert answer_hit_cap(chunks[-1], CAP) is (stop == "length")
