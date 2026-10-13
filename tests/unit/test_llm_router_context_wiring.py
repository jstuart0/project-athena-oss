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
from shared.llm_router import LLMRouter, llm_usage_scope  # noqa: E402

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


def prompt_with_estimate(target):
    """A prompt whose composed form (with the /no_think prefix) estimates to `target`."""
    from shared.llm_context import estimate_tokens

    size = 0
    while estimate_tokens(llm_router_module._compose_ollama_prompt("a" * size, "/no_think")) < target:
        size += 1
    return "a" * size


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
        # a strict call's max_tokens (400) beats the row's smaller num_predict (384)
        assert (options["num_batch"], options["mirostat"], options["num_predict"]) == (256, 2, 400)
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

    def test_unknown_floor_small_need_sends_no_size_and_records_nothing(self, monkeypatch):
        wire = Wire(monkeypatch, row={"num_predict": 384})
        wire.generate("short prompt", strict=True)
        assert "num_ctx" not in wire.requests[0]["options"]
        assert wire.router._context_marks.get(MODEL, None) == 0

    def test_unknown_floor_large_need_refuses_before_any_request(self, monkeypatch):
        wire = Wire(monkeypatch, row={"num_predict": 384})
        before = context_count("unknown_default")
        with pytest.raises(ContextBudgetExceeded) as raised:
            wire.generate(strict=True)
        assert raised.value.reason == "floor_unknown"
        assert wire.requests == []
        assert context_count("unknown_default") - before == 1

    def test_unknown_floor_boundary_at_2048(self, monkeypatch):
        # reserve 400 (the caller's max_tokens beats the row's 384): estimate 1648 is need 2048 (sent), 1649 is need 2049 (refused)
        wire = Wire(monkeypatch, row={"num_predict": 384})
        wire.generate(prompt_with_estimate(1648), strict=True)
        assert len(wire.requests) == 1 and "num_ctx" not in wire.requests[0]["options"]
        with pytest.raises(ContextBudgetExceeded) as raised:
            wire.generate(prompt_with_estimate(1649), strict=True)
        assert raised.value.reason == "floor_unknown"
        assert len(wire.requests) == 1

    def test_a_non_power_of_two_server_context_cannot_slip_through(self, monkeypatch):
        # a 6000 server context reports 3002 for a cut prompt: no power-of-two
        # signature, and 3002 is above 35% of the estimate. Refused up front.
        wire = Wire(monkeypatch, row={"num_predict": 384}, responder=lambda body: reply(3002, "length"))
        with pytest.raises(ContextBudgetExceeded) as raised:
            wire.generate(strict=True)
        assert raised.value.reason == "floor_unknown"
        assert wire.requests == []

    def test_unknown_floor_small_prompt_still_raises_on_a_truncated_result(self, monkeypatch):
        wire = Wire(monkeypatch, row={"num_predict": 384}, responder=lambda body: reply(100, "length"))
        with pytest.raises(ContextBudgetExceeded) as raised:
            wire.generate(prompt_with_estimate(600), strict=True)
        assert raised.value.reason == "truncated"

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


class TestTruncatedStrictCall:
    def test_the_mark_does_not_move(self, monkeypatch):
        wire = Wire(monkeypatch, responder=lambda body: reply(1026, "length"))
        with pytest.raises(ContextBudgetExceeded):
            wire.generate(strict=True)
        assert wire.router._context_marks.get(MODEL, 2048) == 0
        wire.generate(strict=False)
        assert wire.requests[-1]["options"]["num_ctx"] == 2048

    def test_the_refusal_keeps_its_token_counts(self, monkeypatch):
        wire = Wire(monkeypatch, responder=lambda body: reply(1026, "length"))
        with llm_usage_scope() as usage:
            with pytest.raises(ContextBudgetExceeded) as raised:
                wire.generate(strict=True)
        assert (raised.value.prompt_eval_count, raised.value.eval_count) == (1026, 5)
        assert (usage.prompt_tokens, usage.completion_tokens) == (1026, 5)
        assert wire.router._metrics[-1]["prompt_tokens"] == 1026

    def test_refusals_before_the_request_add_no_usage(self, monkeypatch):
        configure(monkeypatch, LLM_CONTEXT_CEILING=4096)
        wire = Wire(monkeypatch)
        with llm_usage_scope() as usage:
            with pytest.raises(ContextBudgetExceeded):
                wire.generate(strict=True)
        assert (usage.prompt_tokens, usage.completion_tokens) == (0, 0)
        assert len(wire.router._metrics) == 0

    def test_the_warning_names_the_floor_not_the_row(self, monkeypatch, logs):
        wire = Wire(monkeypatch, responder=lambda body: reply(1026, "length"))
        wire.generate(strict=False)
        entry = [e for e in logs if e["event"] == "llm_prompt_truncation_suspected"][0]
        assert entry["floor_ctx"] == 2048 and "row_ctx" not in entry


class TestUnsupportedBackends:
    @pytest.mark.parametrize("backend_type", ["mlx", "openai", "anthropic", "google"])
    def test_a_strict_call_fails_closed(self, monkeypatch, backend_type):
        wire = Wire(monkeypatch)

        async def backend_config(model):
            return {"backend_type": backend_type, "endpoint_url": "http://x.test", "timeout_seconds": 30}

        async def must_not_run(*args, **kwargs):
            raise AssertionError("backend reached")

        monkeypatch.setattr(wire.router, "_get_backend_config", backend_config)
        for name in ("_generate_mlx", "_generate_openai", "_generate_anthropic", "_generate_google"):
            monkeypatch.setattr(wire.router, name, must_not_run)
        with pytest.raises(ContextBudgetExceeded) as raised:
            wire.generate(strict=True)
        assert raised.value.reason == "unsupported_backend"
        assert wire.requests == []

    def test_a_default_call_still_reaches_the_backend(self, monkeypatch):
        wire = Wire(monkeypatch)

        async def backend_config(model):
            return {"backend_type": "mlx", "endpoint_url": "http://x.test", "timeout_seconds": 30}

        async def mlx(*args, **kwargs):
            return {"response": "ok", "backend": "mlx"}

        monkeypatch.setattr(wire.router, "_get_backend_config", backend_config)
        monkeypatch.setattr(wire.router, "_generate_mlx", mlx)
        assert wire.generate(strict=False)["backend"] == "mlx"


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

    def test_a_sticky_default_call_keeps_the_retry(self, monkeypatch):
        monkeypatch.setattr(llm_router_module, "OLLAMA_READ_TIMEOUT_RETRY_BACKOFF_S", 0)
        wire = Wire(monkeypatch)
        wire.generate(strict=True)
        wire.requests.clear()
        wire.responder = self.timeout_then_ok(wire.requests)
        wire.generate(strict=False)
        assert [r["options"]["num_ctx"] for r in wire.requests] == [8192, 8192]

    def test_observe_keeps_the_retry(self, monkeypatch):
        configure(monkeypatch, LLM_CONTEXT_GUARD="observe")
        monkeypatch.setattr(llm_router_module, "OLLAMA_READ_TIMEOUT_RETRY_BACKOFF_S", 0)
        wire = Wire(monkeypatch)
        wire.responder = self.timeout_then_ok(wire.requests)
        wire.generate(strict=True)
        assert len(wire.requests) == 2

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
        wire.generate(strict=False)
        assert wire.requests[0]["options"]["num_ctx"] == 2048

    def test_a_strict_auto_call_skips_mlx_and_is_planned_on_ollama(self, monkeypatch):
        configure(monkeypatch, OLLAMA_URL="http://ollama.test")
        wire = Wire(monkeypatch)

        async def backend_config(model):
            return {"backend_type": "auto", "endpoint_url": "http://mlx.test", "timeout_seconds": 30}

        mlx_calls = []

        async def mlx(*args, **kwargs):
            mlx_calls.append(1)
            raise RuntimeError("mlx down")

        monkeypatch.setattr(wire.router, "_get_backend_config", backend_config)
        monkeypatch.setattr(wire.router, "_generate_mlx", mlx)
        wire.generate(strict=True)
        assert mlx_calls == []
        assert wire.requests[0]["options"]["num_ctx"] == 8192


def tool_reply(prompt_eval_count=500, load_duration=123456):
    return httpx.Response(
        200,
        json={
            "message": {"role": "assistant", "content": "ok"}, "done": True, "done_reason": "stop",
            "prompt_eval_count": prompt_eval_count, "eval_count": 5, "load_duration": load_duration,
        },
    )


class TestToolPathDetection:
    """D5: the tool path sends no num_ctx and only detects."""

    @staticmethod
    def tools(wire, prompt=MARKER):
        return run(
            wire.router.generate_with_tools(
                model=MODEL, messages=[{"role": "user", "content": prompt}], tools=[], backend="ollama",
            )
        )

    @pytest.mark.parametrize(
        "error",
        [
            {"error": {"type": "exceed_context_size_error", "message": "the request exceeds the available context size"}},
            {"error": {"message": "request exceeds the available context size (MARKER-BODY)"}},
            {"error": "the request exceeds the available context size: " + MARKER},
        ],
        ids=["type", "message", "string"],
    )
    def test_a_context_400_is_counted_logged_without_the_body_and_reraised(self, monkeypatch, logs, error):
        wire = Wire(monkeypatch, responder=lambda body: httpx.Response(400, json=error))
        before = context_count("rejected_by_backend", path="tools")
        with pytest.raises(httpx.HTTPStatusError):
            self.tools(wire)
        assert context_count("rejected_by_backend", path="tools") - before == 1
        rejected = [e for e in logs if e["event"] == "llm_context_rejected_by_backend"]
        assert len(rejected) == 1 and rejected[0]["path"] == "tools"
        assert MARKER not in json.dumps(logs, default=str) and "MARKER-BODY" not in json.dumps(logs, default=str)
        assert len(wire.requests) == 1, "nothing is retried or resized"
        assert "num_ctx" not in wire.requests[0]["options"]

    @pytest.mark.parametrize(
        "status,error",
        [
            (400, {"error": "invalid request"}),
            (400, {"detail": "no error key"}),
            (500, {"error": "the request exceeds the available context size"}),
        ],
        ids=["other-400", "no-error-key", "500-with-marker"],
    )
    def test_other_failures_are_not_classified_as_context(self, monkeypatch, status, error):
        wire = Wire(monkeypatch, responder=lambda body: httpx.Response(status, json=error))
        before = context_count("rejected_by_backend", path="tools")
        with pytest.raises(httpx.HTTPStatusError):
            self.tools(wire)
        assert context_count("rejected_by_backend", path="tools") == before

    def test_a_non_json_400_is_not_classified_and_still_raises(self, monkeypatch):
        wire = Wire(monkeypatch, responder=lambda body: httpx.Response(400, text="exceeds the available context size"))
        before = context_count("rejected_by_backend", path="tools")
        with pytest.raises(httpx.HTTPStatusError):
            self.tools(wire)
        assert context_count("rejected_by_backend", path="tools") == before

    def test_a_cut_prompt_against_a_known_server_default_is_reported(self, monkeypatch, logs):
        configure(monkeypatch, LLM_SERVER_DEFAULT_CONTEXT=4096)
        # rule (a): the count sits on 4096 // 2 + 2 while the estimate is above 4096
        wire = Wire(monkeypatch, responder=lambda body: tool_reply(2050))
        before = context_count("truncation_suspected", path="tools")
        result = self.tools(wire, prompt="a" * 14000)
        assert result["content"] == "ok", "detection only: the answer is returned"
        assert context_count("truncation_suspected", path="tools") - before == 1
        entry = next(e for e in logs if e["event"] == "llm_prompt_truncation_suspected")
        assert entry["path"] == "tools" and entry["load_duration"] == 123456 and entry["prompt_eval_count"] == 2050
        assert "num_ctx" not in wire.requests[0]["options"]

    def test_a_whole_prompt_is_not_reported(self, monkeypatch):
        configure(monkeypatch, LLM_SERVER_DEFAULT_CONTEXT=4096)
        wire = Wire(monkeypatch, responder=lambda body: tool_reply(4000))
        before = context_count("truncation_suspected", path="tools")
        self.tools(wire, prompt="a" * 14000)
        assert context_count("truncation_suspected", path="tools") == before

    def test_with_no_known_server_default_nothing_is_reported(self, monkeypatch):
        # 100 reported tokens against ~5,000 estimated is a cut prompt by the ratio rule
        # (as the previous test shows with a known default), so only the unknown
        # default keeps it from being reported
        wire = Wire(monkeypatch, responder=lambda body: tool_reply(100))
        before = context_count("truncation_suspected", path="tools")
        self.tools(wire, prompt="a" * 14000)
        assert context_count("truncation_suspected", path="tools") == before

    def test_the_ratio_rule_reports_with_a_known_default(self, monkeypatch):
        configure(monkeypatch, LLM_SERVER_DEFAULT_CONTEXT=65536)
        wire = Wire(monkeypatch, responder=lambda body: tool_reply(100))
        before = context_count("truncation_suspected", path="tools")
        self.tools(wire, prompt="a" * 14000)
        assert context_count("truncation_suspected", path="tools") - before == 1


class TestPrewarmAndContextFor:
    @staticmethod
    def prewarm(wire, models, **kwargs):
        return run(llm_router_module.prewarm_ollama_models(models, **kwargs))

    def test_prewarm_sends_the_context_the_real_calls_send(self, monkeypatch):
        wire = Wire(monkeypatch)
        assert self.prewarm(wire, [MODEL], num_ctx_by_model={MODEL: 8192}) == {MODEL: True}
        assert wire.requests[0]["options"] == {"num_predict": 1, "num_ctx": 8192}

    @pytest.mark.parametrize("value", [None, 0, -5, True, "8192"], ids=repr)
    def test_prewarm_ignores_a_missing_or_invalid_size(self, monkeypatch, value):
        wire = Wire(monkeypatch)
        self.prewarm(wire, [MODEL], num_ctx_by_model={MODEL: value})
        assert wire.requests[0]["options"] == {"num_predict": 1}

    def test_prewarm_without_a_map_is_unchanged(self, monkeypatch):
        wire = Wire(monkeypatch)
        self.prewarm(wire, [MODEL, "other:1b"])
        assert [r["options"] for r in wire.requests] == [{"num_predict": 1}, {"num_predict": 1}]

    def test_context_for_is_the_row_until_a_strict_call_makes_a_larger_size_sticky(self, monkeypatch):
        wire = Wire(monkeypatch)
        assert run(wire.router.context_for(MODEL)) == 2048
        wire.generate(strict=True)
        assert run(wire.router.context_for(MODEL)) == 8192, "the high-water mark beats the row"
        assert run(wire.router.context_for("other:1b")) == 2048, "marks are per model"

    def test_context_for_falls_back_to_the_server_default_and_is_none_when_unknown(self, monkeypatch):
        wire = Wire(monkeypatch, row={"num_predict": 384})
        assert run(wire.router.context_for(MODEL)) is None
        configure(monkeypatch, LLM_SERVER_DEFAULT_CONTEXT=65536)
        assert run(wire.router.context_for(MODEL)) == 65536

    def test_context_for_sends_nothing(self, monkeypatch):
        wire = Wire(monkeypatch)
        run(wire.router.context_for(MODEL))
        assert wire.requests == []


class TestRaiseAboveTheRowWarning:
    """I-1: two replicas size a raised model independently; the operator is told once."""

    def raised(self, logs):
        return [e for e in logs if e["event"] == "llm_context_raised_above_row"]

    def test_a_strict_raise_above_the_row_warns_once_per_model_without_prompt_text(self, monkeypatch, logs):
        wire = Wire(monkeypatch)
        wire.generate(strict=True)
        wire.generate(strict=True)
        entries = self.raised(logs)
        assert len(entries) == 1 and entries[0]["log_level"] == "warning"
        assert (entries[0]["model"], entries[0]["floor"], entries[0]["planned"]) == (MODEL, 2048, 8192)
        assert MARKER not in json.dumps(logs, default=str)
        wire.generate(strict=True, model="other:8b")
        assert len(self.raised(logs)) == 2

    def test_no_warning_when_the_row_already_holds_the_prompt(self, monkeypatch, logs):
        wire = Wire(monkeypatch, row={**ROW_2048, "num_ctx": 8192})
        wire.generate(strict=True)
        assert self.raised(logs) == []

    def test_no_warning_for_a_default_call_or_in_observe_mode(self, monkeypatch, logs):
        wire = Wire(monkeypatch)
        wire.generate(strict=False)
        assert self.raised(logs) == []

    def test_no_warning_in_observe_mode(self, monkeypatch, logs):
        configure(monkeypatch, LLM_CONTEXT_GUARD="observe")
        wire = Wire(monkeypatch)
        wire.generate(strict=True)
        assert self.raised(logs) == []

    def test_the_raise_is_visible_in_the_context_metric(self, monkeypatch):
        wire = Wire(monkeypatch)
        before = context_count("raised")
        wire.generate(strict=True)
        assert context_count("raised") - before == 1


class TestStrictCallersOutputLimit:
    """I-2: a strict caller's max_tokens beats a smaller row num_predict."""

    def test_a_384_row_sends_the_callers_1500(self, monkeypatch):
        wire = Wire(monkeypatch)
        wire.generate(strict=True)
        assert wire.requests[0]["options"]["num_predict"] == 400  # generate() helper passes max_tokens=400
        run(wire.router.generate(model=MODEL, prompt=SH_INTENT_PROMPT, temperature=0.1, max_tokens=1500,
                                 system_prompt="/no_think", require_full_prompt=True))
        assert wire.requests[1]["options"]["num_predict"] == 1500

    def test_a_default_call_keeps_the_row_value(self, monkeypatch):
        wire = Wire(monkeypatch)
        run(wire.router.generate(model=MODEL, prompt="hi", temperature=0.1, max_tokens=1500, system_prompt="/no_think"))
        assert wire.requests[0]["options"]["num_predict"] == 384

    def test_a_row_value_above_the_callers_stays(self, monkeypatch):
        wire = Wire(monkeypatch, row={**ROW_2048, "num_predict": 900})
        run(wire.router.generate(model=MODEL, prompt="hi", temperature=0.1, max_tokens=400,
                                 system_prompt="/no_think", require_full_prompt=True))
        assert wire.requests[0]["options"]["num_predict"] == 900

    def test_the_context_reserve_counts_the_larger_limit(self, monkeypatch):
        # estimate 7000 + 384 fits 8192; + 1500 does not
        wire = Wire(monkeypatch)
        run(wire.router.generate(model=MODEL, prompt=prompt_with_estimate(7000), temperature=0.1, max_tokens=1500,
                                 system_prompt="/no_think", require_full_prompt=True))
        assert wire.requests[0]["options"]["num_ctx"] == 16384

    def test_the_stream_payload_is_unchanged(self, monkeypatch):
        stream_body = '{"response":"ok","done":true,"prompt_eval_count":4441,"eval_count":5}\n'
        wire = Wire(monkeypatch, responder=lambda body: httpx.Response(200, text=stream_body))
        wire.stream()
        assert wire.requests[0]["options"]["num_predict"] == 384


class AdminOutage:
    """A router whose admin model-config endpoint can be switched to fail, on a
    clock the test moves."""

    def __init__(self, monkeypatch, row=None, **wire_kwargs):
        self.wire = Wire(monkeypatch, row=row, **wire_kwargs)
        self.router = self.wire.router
        self.mode = "ok"
        self.fetches = 0
        self.now = [1_000_000.0]
        row_now = lambda: self.wire.row  # noqa: E731

        def handler(request):
            self.fetches += 1
            if self.mode == "down":
                raise httpx.ConnectError("admin down")
            if self.mode in ("503", "401", "403", "404"):
                return httpx.Response(int(self.mode), json={"detail": "x"})
            return httpx.Response(200, json={"model_name": MODEL, "ollama_options": row_now(), "timeout_seconds": 30})

        self.router.client = _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))
        self.router._cache_ttl = 0
        self.router._get_model_config = llm_router_module.LLMRouter._get_model_config.__get__(self.router)
        monkeypatch.setattr(llm_router_module.time, "time", lambda: self.now[0])

    def config(self, model=MODEL):
        return run(self.router._get_model_config(model))

    def advance(self, seconds):
        self.now[0] += seconds


class TestAdminOutageKeepsTheLastRow:
    """I-3: a fetch failure serves the last good config, never an empty one."""

    @pytest.mark.parametrize("mode", ["down", "503"])
    def test_an_outage_after_a_good_fetch_keeps_the_row_and_a_strict_call_works(self, monkeypatch, logs, mode):
        admin = AdminOutage(monkeypatch)
        assert admin.config()["ollama_options"]["num_ctx"] == 2048
        admin.mode = mode
        admin.advance(5)
        assert admin.config()["ollama_options"]["num_ctx"] == 2048
        admin.advance(31)
        admin.config()
        stale = [e for e in logs if e["event"] == "model_config_served_stale"]
        assert len(stale) == 1 and stale[0]["log_level"] == "warning" and stale[0]["model"] == MODEL
        result = admin.wire.generate(strict=True)
        assert result["response"] == "ok"
        assert admin.wire.requests[-1]["options"]["num_ctx"] == 8192

    def test_an_outage_with_no_prior_fetch_keeps_todays_empty_config_and_refuses(self, monkeypatch):
        admin = AdminOutage(monkeypatch)
        admin.mode = "down"
        assert admin.config()["ollama_options"] == {}
        with pytest.raises(ContextBudgetExceeded) as raised:
            admin.wire.generate(strict=True)
        assert raised.value.reason == "floor_unknown"

    def test_a_missing_row_is_not_served_stale(self, monkeypatch):
        admin = AdminOutage(monkeypatch)
        admin.config()
        admin.mode = "404"
        assert admin.config()["ollama_options"] == {}

    def test_the_cache_is_replaced_by_every_success_and_the_warning_rearms(self, monkeypatch, logs):
        admin = AdminOutage(monkeypatch)
        admin.config()
        admin.wire.row = {**ROW_2048, "num_ctx": 4096}
        admin.config()
        admin.mode = "down"
        admin.advance(40)
        assert admin.config()["ollama_options"]["num_ctx"] == 4096
        admin.mode = "ok"
        admin.advance(40)
        admin.config()
        admin.mode = "down"
        admin.advance(40)
        admin.config()
        assert len([e for e in logs if e["event"] == "model_config_served_stale"]) == 2

    def test_the_stale_cache_is_per_model(self, monkeypatch):
        admin = AdminOutage(monkeypatch)
        admin.config()
        admin.mode = "down"
        assert admin.config("other:1b")["ollama_options"] == {}


class TestStaleConfigIsBounded:
    """X-L5: age limit, a short retry hold, no stale on auth refusal, a counter."""

    def test_past_ten_minutes_it_is_no_config_at_all(self, monkeypatch, logs):
        admin = AdminOutage(monkeypatch)
        admin.config()
        admin.mode = "down"
        admin.advance(599)
        assert admin.config()["ollama_options"]["num_ctx"] == 2048
        admin.advance(2)
        assert admin.config()["ollama_options"] == {}
        assert [e["event"] for e in logs].count("model_config_stale_expired") == 1
        admin.advance(40)
        admin.config()
        assert [e["event"] for e in logs].count("model_config_stale_expired") == 1, "one warning per outage"
        with pytest.raises(ContextBudgetExceeded):
            admin.wire.generate(strict=True)

    def test_the_expiry_warning_rearms_after_a_recovery(self, monkeypatch, logs):
        admin = AdminOutage(monkeypatch)
        admin.config()
        admin.mode = "down"
        admin.advance(700)
        admin.config()
        admin.mode = "ok"
        admin.advance(40)
        admin.config()
        admin.mode = "down"
        admin.advance(700)
        admin.config()
        assert [e["event"] for e in logs].count("model_config_stale_expired") == 2

    def test_recovery_after_expiry_restores_the_row(self, monkeypatch):
        admin = AdminOutage(monkeypatch)
        admin.config()
        admin.mode = "down"
        admin.advance(700)
        assert admin.config()["ollama_options"] == {}
        admin.mode = "ok"
        admin.advance(40)
        assert admin.config()["ollama_options"]["num_ctx"] == 2048

    def test_a_stale_serve_holds_the_cache_for_30_seconds(self, monkeypatch):
        admin = AdminOutage(monkeypatch)
        admin.config()
        admin.mode = "down"
        admin.advance(1)
        admin.config()
        fetches = admin.fetches
        admin.advance(29)
        for _ in range(5):
            admin.config()
        assert admin.fetches == fetches, "no admin call within the hold"
        admin.advance(2)
        admin.config()
        assert admin.fetches == fetches + 1

    def test_the_hold_does_not_extend_the_age_limit(self, monkeypatch):
        admin = AdminOutage(monkeypatch)
        admin.config()
        admin.mode = "down"
        for _ in range(25):
            admin.advance(29)
            admin.config()
        assert admin.config()["ollama_options"] == {}, "725 s of serves never refreshed the age"

    @pytest.mark.parametrize("status", ["401", "403"])
    def test_an_auth_refusal_is_not_served_stale_and_is_logged_with_the_status_only(self, monkeypatch, logs, status):
        admin = AdminOutage(monkeypatch)
        admin.config()
        admin.mode = status
        admin.advance(5)
        before = sample("athena_llm_context_total", path="admin", outcome="served_stale", model=MODEL)
        assert admin.config()["ollama_options"] == {}
        assert sample("athena_llm_context_total", path="admin", outcome="served_stale", model=MODEL) == before
        refused = [e for e in logs if e["event"] == "model_config_fetch_refused"]
        assert len(refused) == 1 and refused[0]["status"] == int(status) and refused[0]["log_level"] == "warning"
        assert set(refused[0]) <= {"event", "log_level", "model", "status"}

    def test_each_stale_serve_is_counted(self, monkeypatch):
        admin = AdminOutage(monkeypatch)
        admin.config()
        admin.mode = "down"
        before = sample("athena_llm_context_total", path="admin", outcome="served_stale", model=MODEL)
        for _ in range(3):
            admin.advance(31)
            admin.config()
        assert sample("athena_llm_context_total", path="admin", outcome="served_stale", model=MODEL) - before == 3

    def test_an_expired_serve_is_not_counted_as_served(self, monkeypatch):
        admin = AdminOutage(monkeypatch)
        admin.config()
        admin.mode = "down"
        admin.advance(700)
        before = sample("athena_llm_context_total", path="admin", outcome="served_stale", model=MODEL)
        admin.config()
        assert sample("athena_llm_context_total", path="admin", outcome="served_stale", model=MODEL) == before


class TestStartupWarning:
    def test_unset_server_default_warns(self, monkeypatch, logs):
        assert llm_router_module.warn_if_server_default_context_unknown() is True
        entry = next(e for e in logs if e["event"] == "llm_server_default_context_unset")
        assert entry["log_level"] == "warning"

    def test_a_set_server_default_is_silent(self, monkeypatch, logs):
        configure(monkeypatch, LLM_SERVER_DEFAULT_CONTEXT=65536)
        assert llm_router_module.warn_if_server_default_context_unknown() is False
        assert [e for e in logs if e["event"] == "llm_server_default_context_unset"] == []

    def test_the_lifespan_calls_it_and_prewarm_passes_the_sizes(self):
        import ast
        from pathlib import Path

        tree = ast.parse((Path(__file__).resolve().parents[2] / "src" / "orchestrator" / "main.py").read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        named = lambda n, call: getattr(call.func, "id", getattr(call.func, "attr", None)) == n  # noqa: E731
        assert any(named("warn_if_server_default_context_unknown", c) for c in calls)
        prewarm = [c for c in calls if named("prewarm_ollama_models", c)]
        assert len(prewarm) == 1 and any(k.arg == "num_ctx_by_model" for k in prewarm[0].keywords)
        assert any(named("context_for", c) for c in calls)
