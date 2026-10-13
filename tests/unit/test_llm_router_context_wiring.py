"""LLMRouter <-> context planner wiring.

Real httpx request building, faked only at the socket (`httpx.MockTransport`);
every assertion on what was sent is on the decoded request JSON. The planner's
own rules are in `test_llm_context_plan.py`.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import sys

import httpx
import pytest
from prometheus_client import REGISTRY

sys.path.insert(0, "src")

import shared.llm_router as llm_router_module  # noqa: E402
from shared.config import _clear_cache_for_tests  # noqa: E402
from shared.llm_context import ContextBudgetExceeded  # noqa: E402
from shared.llm_router import LLMRouter  # noqa: E402

_REAL_ASYNC_CLIENT = httpx.AsyncClient

MARKER = "PLANTED-PROMPT-MARKER-7f3a"
SH_INTENT_PROMPT = MARKER + "a" * (12919 - len(MARKER))
ROW_2048 = {"num_ctx": 2048, "num_batch": 256, "mirostat": 2, "num_predict": 384, "mirostat_eta": 0.1, "mirostat_tau": 5.0}
MODEL = "qwen3:8b"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def fresh_config(monkeypatch):
    for name in ("LLM_CONTEXT_GUARD", "LLM_CONTEXT_CEILING", "LLM_SERVER_DEFAULT_CONTEXT"):
        monkeypatch.delenv(name, raising=False)
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


class RecordingLogger:
    """Stands in for the module logger: structlog's capture_logs can't see a
    logger another test already bound and cached (cache_logger_on_first_use)."""

    def __init__(self):
        self.entries = []

    def __getattr__(self, level):
        def log(event, **fields):
            self.entries.append({"event": event, "log_level": level, **fields})
        return log


@pytest.fixture
def logs(monkeypatch):
    recorder = RecordingLogger()
    monkeypatch.setattr(llm_router_module, "logger", recorder)
    return recorder.entries


def configure(monkeypatch, **env):
    for name, value in env.items():
        monkeypatch.setenv(name, str(value))
    _clear_cache_for_tests()


def reply(prompt_eval_count=4441, done_reason="stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={"response": "ok", "done": True, "prompt_eval_count": prompt_eval_count, "eval_count": 5, "done_reason": done_reason},
    )


class Wire:
    """A router whose Ollama socket is a recording handler."""

    def __init__(self, monkeypatch, *, row=None, responder=None):
        self.requests = []
        self.responder = responder or (lambda body: reply())
        self.row = dict(ROW_2048) if row is None else row

        def handler(request):
            body = json.loads(request.content)
            self.requests.append(body)
            return self.responder(body)

        def factory(*args, **kwargs):
            kwargs.pop("transport", None)
            return _REAL_ASYNC_CLIENT(*args, **kwargs, transport=httpx.MockTransport(handler))

        monkeypatch.setattr(llm_router_module.httpx, "AsyncClient", factory)
        self.router = LLMRouter(admin_url="http://admin.test", persist_metrics=False)

        async def backend_config(model):
            return {"backend_type": "ollama", "endpoint_url": "http://ollama.test", "timeout_seconds": 30}

        async def model_config(model):
            return {"ollama_options": self.row, "timeout_seconds": 30, "keep_alive_seconds": -1}

        monkeypatch.setattr(self.router, "_get_backend_config", backend_config)
        monkeypatch.setattr(self.router, "_get_model_config", model_config)

    def generate(self, prompt=SH_INTENT_PROMPT, *, strict=False, model=MODEL, **kwargs):
        return run(
            self.router.generate(
                model=model, prompt=prompt, temperature=0.1, max_tokens=400,
                system_prompt="/no_think", require_full_prompt=strict, **kwargs,
            )
        )

    def stream(self, prompt=SH_INTENT_PROMPT, *, model=MODEL):
        async def consume():
            return [c async for c in self.router._generate_ollama_stream(
                endpoint_url="http://ollama.test", model=model, prompt=prompt, temperature=0.1,
                max_tokens=400, timeout=30, keep_alive=-1, ollama_options=self.row, system_prompt="/no_think",
            )]
        return run(consume())


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def context_count(outcome, path="generate", model=MODEL):
    return sample("athena_llm_context_total", path=path, outcome=outcome, model=model)


def changes(model=MODEL):
    return sample("athena_llm_context_changes_total", model=model)


class TestStrictSizing:
    def test_sh_intent_on_a_2048_row_sends_8192_nested_under_options(self, monkeypatch):
        wire = Wire(monkeypatch)
        wire.generate(strict=True)
        body = wire.requests[0]
        assert "num_ctx" not in body
        options = body["options"]
        assert options["num_ctx"] == 8192
        assert (options["num_batch"], options["mirostat"], options["num_predict"]) == (256, 2, 384)
        assert (options["mirostat_eta"], options["mirostat_tau"]) == (0.1, 5.0)
        assert body["prompt"].startswith("/no_think\n\n" + MARKER)

    def test_a_default_call_sends_the_row_then_the_sticky_size(self, monkeypatch):
        wire = Wire(monkeypatch)
        wire.generate(strict=False)
        wire.generate(strict=True)
        wire.generate(strict=False)
        assert [r["options"]["num_ctx"] for r in wire.requests] == [2048, 8192, 8192]

    def test_a_default_call_alone_never_raises_the_context(self, monkeypatch):
        wire = Wire(monkeypatch, responder=lambda body: reply(4441))
        wire.generate(strict=False)
        assert wire.requests[0]["options"]["num_ctx"] == 2048
        assert wire.router._context_marks.get(MODEL, 2048) == 0

    def test_strict_call_records_the_mark(self, monkeypatch):
        wire = Wire(monkeypatch)
        wire.generate(strict=True)
        assert wire.router._context_marks.get(MODEL, 2048) == 8192

    def test_generate_and_stream_payloads_are_equal_before_and_after_a_raise(self, monkeypatch):
        stream_body = '{"response":"ok","done":true,"prompt_eval_count":4441,"eval_count":5}\n'
        wire = Wire(monkeypatch, responder=lambda body: httpx.Response(200, text=stream_body) if body["stream"] else reply())
        wire.generate(strict=False)
        wire.stream()
        wire.generate(strict=True)
        wire.generate(strict=False)
        wire.stream()
        first, stream_first, _, second, stream_second = wire.requests
        assert {k: v for k, v in first.items() if k != "stream"} == {k: v for k, v in stream_first.items() if k != "stream"}
        assert {k: v for k, v in second.items() if k != "stream"} == {k: v for k, v in stream_second.items() if k != "stream"}
        assert stream_second["options"]["num_ctx"] == 8192

    def test_raise_is_counted_and_changes_counter_moves_once(self, monkeypatch):
        wire = Wire(monkeypatch)
        raised_before, changes_before = context_count("raised"), changes()
        wire.generate(strict=False)  # 2048
        wire.generate(strict=True)  # 8192
        wire.generate(strict=True)  # 8192 again
        assert context_count("raised") - raised_before == 1
        assert changes() - changes_before == 1

    def test_unknown_floor_sends_no_size_and_records_nothing(self, monkeypatch):
        row = {"num_predict": 384}
        wire = Wire(monkeypatch, row=row)
        wire.generate(strict=True)
        assert "num_ctx" not in wire.requests[0]["options"]
        assert wire.router._context_marks.get(MODEL, None) == 0

    def test_server_default_is_the_floor_when_the_row_has_no_size(self, monkeypatch):
        configure(monkeypatch, LLM_SERVER_DEFAULT_CONTEXT=65536)
        wire = Wire(monkeypatch, row={"num_predict": 384})
        wire.generate(strict=True)
        assert wire.requests[0]["options"]["num_ctx"] == 65536


class TestRefusals:
    def test_over_the_ceiling_makes_no_http_request(self, monkeypatch):
        configure(monkeypatch, LLM_CONTEXT_CEILING=4096)
        wire = Wire(monkeypatch)
        before = context_count("over_ceiling")
        with pytest.raises(ContextBudgetExceeded) as raised:
            wire.generate(strict=True)
        assert wire.requests == []
        assert raised.value.reason == "over_ceiling"
        assert context_count("over_ceiling") - before == 1

    def test_a_cut_prompt_raises_for_a_strict_call_and_is_counted(self, monkeypatch, logs):
        wire = Wire(monkeypatch, responder=lambda body: reply(1026, "length"))
        before = context_count("truncation_suspected")
        with pytest.raises(ContextBudgetExceeded) as raised:
            wire.generate(strict=True)
        assert raised.value.reason == "truncated"
        assert context_count("truncation_suspected") - before == 1
        suspected = [entry for entry in logs if entry["event"] == "llm_prompt_truncation_suspected"]
        assert len(suspected) == 1
        assert suspected[0]["log_level"] == "warning"
        assert suspected[0]["prompt_eval_count"] == 1026
        assert MARKER not in json.dumps(logs, default=str)

    def test_a_cut_prompt_only_warns_for_a_default_call(self, monkeypatch, logs):
        wire = Wire(monkeypatch, responder=lambda body: reply(1026, "length"))
        before = context_count("truncation_suspected")
        result = wire.generate(strict=False)
        assert result["prompt_eval_count"] == 1026
        assert context_count("truncation_suspected") - before == 1
        assert [e for e in logs if e["event"] == "llm_prompt_truncation_suspected"]
        assert MARKER not in json.dumps(logs, default=str)

    def test_unknown_floor_truncation_still_raises_for_a_strict_call(self, monkeypatch):
        wire = Wire(monkeypatch, row={"num_predict": 384}, responder=lambda body: reply(2050, "length"))
        before = context_count("unknown_default")
        with pytest.raises(ContextBudgetExceeded) as raised:
            wire.generate(strict=True)
        assert raised.value.reason == "truncated"
        assert context_count("unknown_default") - before == 1

    def test_stream_reports_a_cut_prompt_in_the_done_branch_and_never_raises(self, monkeypatch):
        body = '{"response":"x"}\n{"response":"","done":true,"prompt_eval_count":1026,"eval_count":1}\n'
        wire = Wire(monkeypatch, responder=lambda _: httpx.Response(200, text=body))
        before = context_count("truncation_suspected", path="stream")
        chunks = wire.stream()
        assert chunks[-1]["done"] is True
        assert context_count("truncation_suspected", path="stream") - before == 1

    def test_the_truncation_signal_ignores_non_ollama_responses(self, monkeypatch):
        wire = Wire(monkeypatch)
        before = context_count("truncation_suspected")
        wire.generate(strict=True)
        assert context_count("truncation_suspected") == before


class TestObserveMode:
    def test_observe_sends_todays_size_and_records_nothing(self, monkeypatch):
        configure(monkeypatch, LLM_CONTEXT_GUARD="observe")
        wire = Wire(monkeypatch)
        before = context_count("raised")
        wire.generate(strict=True)
        assert wire.requests[0]["options"]["num_ctx"] == 2048
        assert wire.router._context_marks.get(MODEL, 2048) == 0
        assert context_count("raised") - before == 1  # computed, counted, not applied

    def test_observe_never_raises_even_over_the_ceiling_or_on_truncation(self, monkeypatch):
        configure(monkeypatch, LLM_CONTEXT_GUARD="observe", LLM_CONTEXT_CEILING=2048)
        wire = Wire(monkeypatch, responder=lambda body: reply(1026, "length"))
        result = wire.generate(strict=True)
        assert result["prompt_eval_count"] == 1026
        assert len(wire.requests) == 1


class TestRetry:
    def timeout_then_ok(self, wire_requests):
        def responder(body):
            if len(wire_requests) == 1:
                raise httpx.ReadTimeout("slow")
            return reply()
        return responder

    def test_no_retry_after_a_raised_plan(self, monkeypatch):
        monkeypatch.setattr(llm_router_module, "OLLAMA_READ_TIMEOUT_RETRY_BACKOFF_S", 0)
        wire = Wire(monkeypatch)
        wire.responder = self.timeout_then_ok(wire.requests)
        with pytest.raises(httpx.ReadTimeout):
            wire.generate(strict=True)
        assert len(wire.requests) == 1

    def test_an_unraised_plan_still_retries_once(self, monkeypatch):
        monkeypatch.setattr(llm_router_module, "OLLAMA_READ_TIMEOUT_RETRY_BACKOFF_S", 0)
        wire = Wire(monkeypatch)
        wire.responder = self.timeout_then_ok(wire.requests)
        wire.generate(strict=False)
        assert len(wire.requests) == 2


class TestChangesCounter:
    def test_counts_2048_to_8192_and_not_8192_to_8192(self, monkeypatch):
        wire = Wire(monkeypatch)
        base = changes()
        wire.generate(strict=False)
        assert changes() == base
        wire.generate(strict=True)
        assert changes() - base == 1
        wire.generate(strict=True)
        wire.generate(strict=False)
        assert changes() - base == 1


class TestRowHandling:
    def test_an_invalid_row_size_is_reported_once_per_model(self, monkeypatch, logs):
        wire = Wire(monkeypatch, row={"num_ctx": "2048", "num_predict": 384})
        wire.generate(strict=False)
        wire.generate(strict=False)
        errors = [e for e in logs if e["event"] == "llm_row_num_ctx_invalid"]
        assert len(errors) == 1 and errors[0]["log_level"] == "error"
        assert "2048" not in json.dumps(errors, default=str)
        # the unvalidated row value is still what today's code sends
        assert wire.requests[0]["options"]["num_ctx"] == "2048"

    def test_a_row_decrease_takes_effect_without_a_restart(self, monkeypatch):
        wire = Wire(monkeypatch)
        wire.generate(strict=True)
        wire.row = {**ROW_2048, "num_ctx": 1024}
        wire.generate(strict=False)
        assert wire.requests[-1]["options"]["num_ctx"] == 1024


class TestSignature:
    def test_require_full_prompt_is_keyword_only_ahead_of_kwargs(self):
        parameters = inspect.signature(LLMRouter.generate).parameters
        assert parameters["require_full_prompt"].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters["require_full_prompt"].default is False
        names = list(parameters)
        assert names.index("require_full_prompt") < names.index("kwargs")

    def test_it_cannot_be_passed_positionally(self, monkeypatch):
        wire = Wire(monkeypatch)
        with pytest.raises(TypeError):
            run(wire.router.generate(MODEL, "p", 0.1, 100, None, None, None, None, None, "/no_think", True))


class TestAutoFallback:
    def test_the_auto_to_ollama_fallback_is_planned_too(self, monkeypatch):
        configure(monkeypatch, OLLAMA_URL="http://ollama.test")
        wire = Wire(monkeypatch)

        async def backend_config(model):
            return {"backend_type": "auto", "endpoint_url": "http://mlx.test", "timeout_seconds": 30}

        async def broken_mlx(*args, **kwargs):
            raise RuntimeError("mlx down")

        monkeypatch.setattr(wire.router, "_get_backend_config", backend_config)
        monkeypatch.setattr(wire.router, "_generate_mlx", broken_mlx)
        wire.generate(strict=True)
        assert wire.requests[0]["options"]["num_ctx"] == 8192
