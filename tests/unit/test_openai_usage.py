"""`/v1/chat/completions` reports the real token usage of the turn, and every stream
route's usage scope is open where the work runs.

The router is the real `LLMRouter` (its backend served by a fake), so the counts
travel the same path they do in production: backend result -> `_add_usage` ->
the request's `llm_usage_scope` -> the response. A fast-path turn makes no LLM
call and reports zeros.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from shared import llm_router
from shared.llm_router import LLMRouter, current_usage

from . import _fast_path_harness as fp
from . import _public_audience_harness as h

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "openai_chat_response_v1.json"
PROMPT, COMPLETION = 321, 12


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


class _Rig:
    def __init__(self, monkeypatch):
        fp.install(monkeypatch, slow="record")
        self.router = LLMRouter(admin_url="http://admin.test", persist_metrics=False)
        h._runtime.set_llm_router(self.router)
        self.seen = []                                   # usage as the generators saw it, after synthesis
        self.monkeypatch = monkeypatch
        sm = h._runtime.get_session_manager()
        real_add = sm.add_message

        async def spy(*args, **kwargs):
            usage = current_usage()
            self.seen.append(None if usage is None else (usage.prompt_tokens, usage.completion_tokens, usage.calls))
            return await real_add(*args, **kwargs)

        monkeypatch.setattr(sm, "add_message", spy)

        async def backend(model, prompt, *a, **k):
            yield {"token": "Tokens streamed. ", "done": False}
            yield {"token": "", "done": True, "eval_count": COMPLETION, "prompt_eval_count": PROMPT, "backend": "ollama"}

        monkeypatch.setattr(self.router, "_stream_backend", backend)

        async def prompt(state):
            return ("p", "m", "s")

        async def run(state):                            # nothing precomputed: the route streams from the LLM
            state.intent = h.IntentCategory.GENERAL_INFO
            state.answer = ""
            return state

        monkeypatch.setattr(h.main, "build_synthesis_prompt_for_streaming", prompt)
        monkeypatch.setattr(h.main, "run_orchestrator_for_streaming", run)

        outer = self

        class _Graph:
            async def ainvoke(self, state):
                # One real router call inside the graph, through a fake Ollama.
                real = httpx.AsyncClient

                def factory(*a, **k):
                    k["transport"] = httpx.MockTransport(lambda r: httpx.Response(200, json={
                        "response": "The answer.", "done": True, "done_reason": "stop",
                        "eval_count": COMPLETION, "prompt_eval_count": PROMPT}))
                    return real(*a, **k)

                monkeypatch.setattr(llm_router.httpx, "AsyncClient", factory)
                monkeypatch.setattr(outer.router, "_get_backend_config", _async_const(
                    {"endpoint_url": "http://o.test", "backend_type": llm_router.BackendType.OLLAMA}))
                monkeypatch.setattr(outer.router, "_get_model_config", _async_const({}))
                await outer.router.generate(model="m", prompt="p")
                return {"intent": h.IntentCategory.GENERAL_INFO, "answer": "The answer.", "confidence": 1.0,
                        "citations": [], "request_id": "r", "node_timings": {}, "validation_passed": True}

        monkeypatch.setattr(h.main, "orchestrator_graph", _Graph())


def _async_const(value):
    async def fn(*args, **kwargs):
        return value

    return fn


# --- /v1 non-stream ---------------------------------------------------------------------------------


def test_v1_non_stream_reports_the_real_counts(monkeypatch):
    rig = _Rig(monkeypatch)
    resp = asyncio.run(fp.send("v1_nonstream", "what is the capital of France"))
    assert resp.status_code == 200
    assert resp.json()["usage"] == {"prompt_tokens": PROMPT, "completion_tokens": COMPLETION, "total_tokens": PROMPT + COMPLETION}


def test_the_outer_scope_sees_the_calls_process_query_made(monkeypatch):
    """process_query opens its own scope; inside chat_completions' scope it reuses it, so the totals reach the response."""
    rig = _Rig(monkeypatch)
    asyncio.run(fp.send("v1_nonstream", "what is the capital of France"))
    assert (PROMPT, COMPLETION, 1) in rig.seen, "process_query's session writes ran inside the one scope"


def test_a_fast_path_turn_reports_zeros(monkeypatch):
    rig = _Rig(monkeypatch)
    resp = asyncio.run(fp.send("v1_nonstream", "hello"))
    assert resp.json()["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def test_the_response_matches_the_golden_file(monkeypatch):
    rig = _Rig(monkeypatch)
    body = asyncio.run(fp.send("v1_nonstream", "what is the capital of France")).json()
    body.update(id="chatcmpl-ID", created=0)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    assert body == json.loads(GOLDEN.read_text(encoding="utf-8"))


# --- every stream route: the scope is open inside the generator, after synthesis ---------------------------------------


def test_query_stream_scope_is_open_after_synthesis(monkeypatch):
    rig = _Rig(monkeypatch)
    asyncio.run(fp.send("query_stream", "tell me about the town", interface_type="text"))
    assert rig.seen == [(PROMPT, COMPLETION, 1), (PROMPT, COMPLETION, 1)]


def test_v1_stream_scope_is_open_after_synthesis(monkeypatch):
    rig = _Rig(monkeypatch)
    asyncio.run(fp.send("v1_stream", "tell me about the town", interface_type="text"))
    assert rig.seen and all(entry == (PROMPT, COMPLETION, 1) for entry in rig.seen)


def test_query_stream_v2_scope_is_open_after_the_graphs_calls(monkeypatch):
    rig = _Rig(monkeypatch)
    asyncio.run(fp.send("query_stream_v2", "tell me about the town", interface_type="text"))
    assert rig.seen and all(entry == (PROMPT, COMPLETION, 1) for entry in rig.seen)


def test_a_query_route_scope_is_open_inside_process_query(monkeypatch):
    rig = _Rig(monkeypatch)
    asyncio.run(fp.send("query", "tell me about the town", interface_type="text"))
    assert rig.seen and all(entry == (PROMPT, COMPLETION, 1) for entry in rig.seen)


@pytest.mark.parametrize("route", ["query_stream", "query_stream_v2", "v1_stream", "query"])
def test_the_scope_is_closed_when_the_request_is_over(monkeypatch, route):
    rig = _Rig(monkeypatch)

    async def go():
        await fp.send(route, "tell me about the town", interface_type="text")
        return current_usage()

    assert asyncio.run(go()) is None


def test_stream_synthesis_rows_carry_the_stage_and_request_labels(monkeypatch):
    rig = _Rig(monkeypatch)
    rows = []
    monkeypatch.setattr(rig.router, "_persist_metric", lambda metric, source=None, stage=None: _record(rows, metric, stage))
    asyncio.run(fp.send("query_stream", "tell me about the town", interface_type="text"))
    assert len(rows) == 1
    assert rows[0][1] == "stream_synthesis" and rows[0][0]["prompt_tokens"] == PROMPT and rows[0][0]["session_id"]


async def _record(rows, metric, stage):
    rows.append((metric, stage))


# --- wiring: where the scope is opened ---------------------------------------------------------------------


def _main_tree():
    import ast

    return ast.parse(h.MAIN_PY.read_text(encoding="utf-8"))


def _function(tree, name):
    import ast

    return next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _decorator_names(fn):
    import ast

    return {ast.unparse(d) for d in fn.decorator_list}


def test_process_query_and_each_stream_generator_open_the_scope_where_the_work_runs():
    tree = _main_tree()
    assert "with_llm_usage_scope" in _decorator_names(_function(tree, "process_query"))
    for name in ("event_generator", "sentence_event_generator", "openai_stream_generator"):
        assert "with_llm_usage_scope_stream" in _decorator_names(_function(tree, name)), name


def test_the_non_streaming_v1_branch_reads_usage_from_an_outer_scope():
    import ast

    fn = _function(_main_tree(), "chat_completions")
    withs = [n for n in ast.walk(fn) if isinstance(n, ast.With) and "llm_usage_scope" in ast.unparse(n.items[0].context_expr)]
    assert len(withs) == 1
    assert "process_query(" in ast.unparse(withs[0])
    assert '"prompt_tokens": 0' not in ast.unparse(fn)


def test_every_stream_synthesis_call_is_labelled():
    import ast

    tree = _main_tree()
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "generate_stream"]
    assert len(calls) >= 2
    for call in calls:
        keywords = {k.arg: ast.unparse(k.value) for k in call.keywords}
        assert keywords.get("stage") == "'stream_synthesis'", call.lineno
        assert "request_id" in keywords and "session_id" in keywords, call.lineno
