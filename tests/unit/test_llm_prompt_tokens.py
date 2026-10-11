"""Prompt-token accounting in the LLM router: metric rows, streams, usage scopes, task retention.

Covers: the exact payload `_persist_metric` posts (golden fixture shared with
the admin test), `generate_with_tools`, a stream's single metric row (also when
the consumer is cancelled), the per-request usage scope (isolation, nesting,
ownership-checked exit, a real LangGraph, two real stream routes with one
cancelled mid-synthesis), and the retained-task drain in `close()`.
"""
from __future__ import annotations

import asyncio
import contextvars
import gc
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import httpx
import pytest
import structlog

sys.path.insert(0, "src")

from shared import llm_router  # noqa: E402
from shared.llm_router import LLMRouter, current_usage, llm_usage_scope  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
GOLDEN = FIXTURES / "llm_metric_payload_v1.json"
GOLDEN_FULL = FIXTURES / "llm_metric_payload_v1_full.json"
GOLDEN_EARLY_STOP = FIXTURES / "llm_metric_payload_v1_stream_early_stop.json"
# What the admin's LLMMetricCreate requires; a row missing any of these is a 422 and never lands.
ADMIN_REQUIRED = ("timestamp", "model", "backend", "latency_seconds", "tokens", "tokens_per_second")


def _router(persist=True):
    return LLMRouter(admin_url="http://admin.test", persist_metrics=persist)


def _http(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(llm_router.httpx, "AsyncClient", factory)


class _Backend:
    """An Ollama + admin pair behind one MockTransport; records every admin POST."""

    def __init__(self, generate_body=None, stream_lines=None):
        self.posts = []
        self.generate_body = generate_body or {
            "response": "Hello there.", "done": True, "done_reason": "stop", "eval_count": 12, "prompt_eval_count": 321,
        }
        self.stream_lines = stream_lines

    def handler(self, request):
        if request.url.path == "/api/llm-backends/metrics":
            self.posts.append(json.loads(request.content))
            return httpx.Response(201, json={"status": "ok"})
        if request.url.path == "/api/generate":
            payload = json.loads(request.content)
            if payload.get("stream"):
                return httpx.Response(200, text="\n".join(json.dumps(x) for x in self.stream_lines))
            return httpx.Response(200, json=self.generate_body)
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "Hi."}, "done": True,
                                             "done_reason": "stop", "eval_count": 5, **{
                                                 k: v for k, v in self.generate_body.items() if k == "prompt_eval_count"}})
        raise AssertionError(f"unexpected request {request.url}")


def _wire(monkeypatch, backend, router):
    _http(monkeypatch, backend.handler)
    monkeypatch.setattr(router, "_get_backend_config", mock.AsyncMock(
        return_value={"endpoint_url": "http://o.test", "backend_type": llm_router.BackendType.OLLAMA}))
    monkeypatch.setattr(router, "_get_model_config", mock.AsyncMock(return_value={}))


def _run(coro):
    return asyncio.run(coro)


# --- the posted payload, as a contract --------------------------------------------------------------


def _generate_and_collect(monkeypatch, body=None, **labels):
    backend = _Backend(generate_body=body)
    router = _router()
    _wire(monkeypatch, backend, router)
    labels = {"request_id": "req-1", "session_id": "sess-1", **labels}

    async def go():
        await router.generate(model="m", prompt="p", stage="synthesize", **labels)
        await router._drain_pending()

    _run(go())
    assert len(backend.posts) == 1
    return backend.posts[0]


def _shape(payload):
    return {k: type(v).__name__ for k, v in payload.items()}


def _check_against(payload, path):
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(json.dumps({**payload, "timestamp": 1700000000.5, "latency_seconds": 1.25,
                                    "tokens_per_second": 9.6}, indent=2) + "\n", encoding="utf-8")
    golden = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == set(golden), f"key set drifted from {path.name}"
    for key, value in golden.items():
        if value is None or payload[key] is None:
            assert (value is None) == (payload[key] is None), f"{key}: null in one and not the other ({path.name})"
            continue
        assert type(payload[key]) is type(value) or (isinstance(value, (int, float)) and isinstance(payload[key], (int, float))), key


def test_the_posted_payload_matches_the_shared_fixture(monkeypatch):
    payload = _generate_and_collect(monkeypatch)
    _check_against(payload, GOLDEN)
    assert payload["prompt_tokens"] == 321 and payload["tokens"] == 12
    assert payload["stage"] == "synthesize" and payload["source"] == "orchestrator"


def test_a_row_with_every_optional_label_matches_the_full_fixture(monkeypatch):
    payload = _generate_and_collect(monkeypatch, user_id="household-pat", zone="kitchen", intent="general_info",
                                    request_id="req-2", session_id="sess-2")
    _check_against(payload, GOLDEN_FULL)
    assert (payload["user_id"], payload["zone"], payload["intent"]) == ("household-pat", "kitchen", "general_info")


def test_the_fixtures_differ_in_what_they_pin():
    base = json.loads(GOLDEN.read_text(encoding="utf-8"))
    full = json.loads(GOLDEN_FULL.read_text(encoding="utf-8"))
    assert base["user_id"] is None and full["user_id"] and full["zone"] and full["intent"]


@pytest.mark.parametrize("body,expected", [
    ({"response": "x", "done": True, "eval_count": 3, "prompt_eval_count": 0}, 0),
    ({"response": "x", "done": True, "eval_count": 3}, None),
    ({"response": "x", "done": True, "eval_count": 3, "prompt_eval_count": 5000}, 5000),
])
def test_zero_is_zero_and_missing_is_null(monkeypatch, body, expected):
    payload = _generate_and_collect(monkeypatch, body)
    assert payload["prompt_tokens"] == expected
    assert ("prompt_tokens" in payload) is True


def test_the_tool_calling_path_posts_prompt_tokens(monkeypatch):
    backend = _Backend()
    router = _router()
    monkeypatch.setattr(router, "_get_backend_config", mock.AsyncMock(return_value={"backend_type": "ollama"}))
    _http(monkeypatch, backend.handler)

    async def go():
        result = await router.generate_with_tools(
            model="m", messages=[{"role": "user", "content": "x"}], tools=[], backend="ollama", stage="tool_selection")
        await router._drain_pending()
        return result

    result = _run(go())
    assert result["prompt_eval_count"] == 321
    assert backend.posts[0]["prompt_tokens"] == 321 and backend.posts[0]["stage"] == "tool_selection"


def test_the_tool_calling_path_adds_to_the_open_scope(monkeypatch):
    backend = _Backend()
    router = _router(persist=False)
    monkeypatch.setattr(router, "_get_backend_config", mock.AsyncMock(return_value={"backend_type": "ollama"}))
    _http(monkeypatch, backend.handler)

    async def go():
        with llm_usage_scope() as usage:
            await router.generate_with_tools(model="m", messages=[{"role": "user", "content": "x"}], tools=[], backend="ollama")
        return usage

    usage = _run(go())
    assert (usage.prompt_tokens, usage.completion_tokens, usage.calls) == (321, 5, 1)


def test_a_plain_generate_adds_to_the_open_scope(monkeypatch):
    backend = _Backend()
    router = _router(persist=False)
    _wire(monkeypatch, backend, router)

    async def go():
        with llm_usage_scope() as usage:
            await router.generate(model="m", prompt="p")
            await router.generate(model="m", prompt="p")
        return usage

    usage = _run(go())
    assert (usage.prompt_tokens, usage.completion_tokens, usage.calls) == (642, 24, 2)


# --- streams: one row, even when the consumer is cancelled -----------------------------------------------------

STREAM = [
    {"response": "Hello", "done": False},
    {"response": " there", "done": False},
    {"response": "", "done": True, "done_reason": "stop", "eval_count": 12, "prompt_eval_count": 321},
]


def test_a_completed_stream_posts_exactly_one_row_with_the_final_counts(monkeypatch):
    backend = _Backend(stream_lines=STREAM)
    router = _router()
    _wire(monkeypatch, backend, router)

    async def go():
        chunks = [c async for c in router.generate_stream(model="m", prompt="p", stage="stream_synthesis",
                                                           request_id="r", session_id="s")]
        await router._drain_pending()
        return chunks

    chunks = _run(go())
    assert chunks[-1]["done"] is True
    assert len(backend.posts) == 1
    row = backend.posts[0]
    assert (row["prompt_tokens"], row["tokens"], row["stage"], row["request_id"], row["session_id"]) == (
        321, 12, "stream_synthesis", "r", "s")


def test_a_consumer_that_breaks_after_the_done_chunk_still_posts_one_row(monkeypatch):
    """main.py's loops `break` on the done chunk and leave the generator suspended."""
    backend = _Backend(stream_lines=STREAM)
    router = _router()
    _wire(monkeypatch, backend, router)

    async def go():
        async for chunk in router.generate_stream(model="m", prompt="p"):
            if chunk.get("done"):
                break
        await asyncio.sleep(0)
        await router._drain_pending()

    _run(go())
    assert len(backend.posts) == 1


def test_a_cancelled_consumer_posts_one_row_and_nothing_is_awaited_in_the_generator(monkeypatch):
    backend = _Backend(stream_lines=STREAM)
    router = _router()
    _wire(monkeypatch, backend, router)
    persist = mock.AsyncMock(wraps=router._persist_metric)
    monkeypatch.setattr(router, "_persist_metric", persist)

    async def go():
        started = asyncio.Event()
        release = asyncio.Event()

        async def consume():
            async for chunk in router.generate_stream(model="m", prompt="p"):
                started.set()
                await release.wait()               # parked mid-stream, as a slow client would be

        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Releasing the abandoned generator runs its `finally` on the loop's next turns.
        gc.collect()
        for _ in range(5):
            await asyncio.sleep(0)
        # The row is spawned, not awaited in the generator: it is pending or done, held by the router.
        assert persist.call_count == 1
        await router._drain_pending()

    _run(go())
    assert len(backend.posts) == 1
    assert backend.posts[0]["prompt_tokens"] is None, "no final chunk arrived, so no count was reported"
    assert backend.posts[0]["tokens"] >= 1


def test_an_early_stop_row_names_its_backend_and_is_complete_enough_for_the_admin(monkeypatch):
    """Without a final chunk the row used to carry backend=None, which the admin rejects (422): the row never landed."""
    backend = _Backend(stream_lines=STREAM)
    router = _router()
    _wire(monkeypatch, backend, router)

    async def go():
        agen = router.generate_stream(model="m", prompt="p", stage="stream_synthesis", request_id="req-3", session_id="sess-1")
        await agen.__anext__()                       # a client that disconnects after the first token
        await agen.aclose()
        await router._drain_pending()

    _run(go())
    assert len(backend.posts) == 1
    row = backend.posts[0]
    assert row["backend"] == "ollama"
    assert all(row.get(key) is not None for key in ADMIN_REQUIRED), row
    _check_against(row, GOLDEN_EARLY_STOP)
    assert None not in router.report_metrics()["by_backend"]


def test_closing_the_generator_early_posts_one_row(monkeypatch):
    backend = _Backend(stream_lines=STREAM)
    router = _router()
    _wire(monkeypatch, backend, router)

    async def go():
        agen = router.generate_stream(model="m", prompt="p")
        await agen.__anext__()
        await agen.aclose()
        await router._drain_pending()

    _run(go())
    assert len(backend.posts) == 1


# --- retained background tasks ------------------------------------------------------------------------------------


def test_spawn_holds_a_strong_reference_until_done():
    router = _router(persist=False)

    async def go():
        release = asyncio.Event()

        async def work():
            await release.wait()

        task = router._spawn(work())
        import gc
        gc.collect()
        assert task in router._pending_tasks
        release.set()
        await task
        await asyncio.sleep(0)
        assert task not in router._pending_tasks

    _run(go())


def test_close_drains_pending_metric_tasks_before_closing_the_client():
    router = _router(persist=False)
    order = []

    async def work():
        await asyncio.sleep(0.05)
        order.append("metric")

    async def go():
        router._spawn(work())
        real_close = router.client.aclose

        async def closing():
            order.append("client_closed")
            await real_close()

        router.client.aclose = closing
        await router.close()

    _run(go())
    assert order == ["metric", "client_closed"]


def test_a_task_that_never_finishes_is_cancelled_after_the_bound(monkeypatch):
    monkeypatch.setattr(llm_router, "BACKGROUND_DRAIN_TIMEOUT_SECONDS", 0.05)
    router = _router(persist=False)

    async def go():
        async def never():
            await asyncio.sleep(3600)

        task = router._spawn(never())
        with structlog.testing.capture_logs() as logs:
            await router.close()
        assert task.cancelled(), "cancelled by close() itself, not by the loop shutting down afterwards"
        return task, logs

    task, logs = _run(go())
    assert {"event": "background_tasks_abandoned", "count": 1, "log_level": "warning"} in logs


# --- usage scope ------------------------------------------------------------------------------------------------------


def test_no_scope_no_accumulation():
    llm_router._add_usage(5, 5)
    assert current_usage() is None


def test_calls_inside_a_scope_accumulate_and_unknown_counts_add_zero():
    with llm_usage_scope() as usage:
        llm_router._add_usage(100, 10)
        llm_router._add_usage(None, 5)
        llm_router._add_usage(0, None)
        assert current_usage() is usage
    assert (usage.prompt_tokens, usage.completion_tokens, usage.calls, usage.total_tokens) == (100, 15, 3, 115)
    assert current_usage() is None


def test_nesting_reuses_the_outer_scope_and_leaves_it_open():
    with llm_usage_scope() as outer:
        llm_router._add_usage(10, 1)
        with llm_usage_scope() as inner:
            assert inner is outer
            llm_router._add_usage(20, 2)
        assert current_usage() is outer, "an inner exit must not reset the outer scope"
        llm_router._add_usage(30, 3)
    assert (outer.prompt_tokens, outer.completion_tokens) == (60, 6)


def test_concurrent_tasks_never_share_a_scope():
    async def worker(n):
        with llm_usage_scope() as usage:
            for _ in range(3):
                await asyncio.sleep(0)
                llm_router._add_usage(n, n)
            return usage

    async def go():
        return await asyncio.gather(worker(1), worker(100))

    a, b = _run(go())
    assert a is not b and (a.prompt_tokens, b.prompt_tokens) == (3, 300)


def test_an_exit_from_a_different_context_does_not_raise_and_restores():
    cm = llm_usage_scope()
    usage = cm.__enter__()
    context = contextvars.copy_context()
    # Exit inside a copy of the context: the token belongs to this one, so reset raises ValueError inside.
    context.run(cm.__exit__, None, None, None)
    assert current_usage() is usage, "the original context was not touched by the foreign exit"
    cm2 = llm_usage_scope()
    assert cm2.__enter__() is usage
    cm2.__exit__(None, None, None)
    assert current_usage() is usage
    llm_router._USAGE.set(None)


def test_an_exit_where_another_scope_is_active_changes_nothing():
    """Ownership check: the active accumulator isn't ours, so leave it alone."""
    cm = llm_usage_scope()
    ours = cm.__enter__()
    other = llm_router.LLMUsage()
    llm_router._USAGE.set(other)
    cm.__exit__(None, None, None)
    assert current_usage() is other and ours is not other
    llm_router._USAGE.set(None)


_GRAPH_SCRIPT = """
import asyncio, operator, sys
from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, END
from shared import llm_router

class S(TypedDict):
    n: int

async def first(state):
    llm_router._add_usage(10, 1)
    return {"n": 1}

async def second(state):
    llm_router._add_usage(20, 2)
    return {"n": 2}

g = StateGraph(S)
g.add_node("first", first)
g.add_node("second", second)
g.set_entry_point("first")
g.add_edge("first", "second")
g.add_edge("second", END)
app = g.compile()

async def main():
    with llm_router.llm_usage_scope() as usage:
        await app.ainvoke({"n": 0})
        print("USAGE", usage.prompt_tokens, usage.completion_tokens, usage.calls)

asyncio.run(main())
"""


def test_a_real_langgraph_run_accumulates_into_the_callers_scope():
    """Fresh interpreter: other test modules stub langgraph in-process."""
    src = str(Path(__file__).resolve().parents[2] / "src")
    proc = subprocess.run([sys.executable, "-c", _GRAPH_SCRIPT], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "PYTHONPATH": src})
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert "USAGE 30 3 2" in proc.stdout


# --- two real stream routes, one cancelled mid-synthesis ---------------------------------------------------------------


def test_a_cancelled_stream_leaves_the_surviving_requests_scope_intact(monkeypatch):
    from . import _fast_path_harness as fp
    from . import _public_audience_harness as h

    fp.install(monkeypatch, slow="record")
    router = _router(persist=False)
    h._runtime.set_llm_router(router)
    gate_a, a_started, b_started, a_cancelled = (asyncio.Event() for _ in range(4))
    accumulators = {}
    seen_at_record = {}

    async def stream_backend(model, prompt, *args, **kwargs):
        who = "A" if "alpha" in prompt else "B"
        accumulators[who] = current_usage()
        yield {"token": f"{who}1 ", "done": False}
        if who == "A":
            a_started.set()
            await gate_a.wait()                     # parked mid-synthesis until it is cancelled
        else:
            b_started.set()
            await a_cancelled.wait()                # B keeps accumulating after A is gone
        yield {"token": "", "done": True, "eval_count": 7, "prompt_eval_count": 40 if who == "B" else 90, "backend": "ollama"}

    real_add = llm_router._add_usage

    def spy_add(prompt, completion):
        seen_at_record[len(seen_at_record)] = (current_usage(), prompt)
        real_add(prompt, completion)

    monkeypatch.setattr(router, "_stream_backend", stream_backend)
    monkeypatch.setattr(llm_router, "_add_usage", spy_add)

    async def prompt(state):
        return (state.query, "model", "system")

    async def run(state):                           # no precomputed answer: the route streams from the LLM
        state.intent = h.IntentCategory.GENERAL_INFO
        state.answer = ""
        return state

    monkeypatch.setattr(h.main, "build_synthesis_prompt_for_streaming", prompt)
    monkeypatch.setattr(h.main, "run_orchestrator_for_streaming", run)

    async def go():
        task_a = asyncio.create_task(fp.send("query_stream", "alpha question please", interface_type="text"))
        task_b = asyncio.create_task(fp.send("query_stream", "bravo question please", interface_type="text"))
        await a_started.wait()
        await b_started.wait()
        task_a.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task_a
        a_cancelled.set()
        response_b = await task_b
        return response_b

    response_b = asyncio.run(go())
    assert response_b.status_code == 200
    acc_b = accumulators["B"]
    assert acc_b is not None and accumulators["A"] is not acc_b
    assert (acc_b.prompt_tokens, acc_b.calls) == (40, 1), "B's totals are exactly its own call"
    recorded_for_b = [scope for scope, prompt in seen_at_record.values() if prompt == 40]
    assert recorded_for_b == [acc_b], "B's call, made after A was cancelled, still saw B's own accumulator"
    assert all(scope is not acc_b for scope, prompt in seen_at_record.values() if prompt == 90)
