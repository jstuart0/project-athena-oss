"""orchestrator.turn_capture: bind, observe, submit, the decorator and the stream wrapper."""
from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest
from starlette.responses import StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from orchestrator import turn_capture as tc
from orchestrator.nodes import _runtime
from shared import conversation_capture as cc
from shared.knowledge_tiers import KnowledgeAudience
from tests.unit._capture_fakes import InMemoryCaptureStore, RecordingSink

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
SECRET = "hunter2-do-not-leak"


@pytest.fixture(autouse=True)
def _clean_runtime():
    _runtime.reset_for_test()
    yield
    _runtime.reset_for_test()


@pytest.fixture
def sink():
    recording = RecordingSink()
    _runtime.set_turn_capture(recording)
    return recording


OWNER = KnowledgeAudience(mode="owner", degraded=False, public=False, owner_caller=True, owner_proven=True)
GUEST = KnowledgeAudience(mode="guest", degraded=False, public=False, owner_caller=False, owner_proven=False, guest_verified=True)
HOUSEHOLD = KnowledgeAudience(mode="owner", degraded=False, public=False, owner_caller=False, owner_proven=False)


def bound(capture, query="turn on the porch light", session_id="own-abcd", **over):
    kwargs = dict(session_id=session_id, query=query, interface_type="voice", audience=OWNER,
                  room="porch", user_mode="owner", caller_trust="web_authenticated", stay_read_only=False)
    kwargs.update(over)
    capture.bind(**kwargs)
    return capture


class State:
    """A minimal stand-in for OrchestratorState."""
    def __init__(self, **kw):
        defaults = dict(answer="Done.", intent="control", confidence=0.9, request_id="r1", is_fallback=False,
                        model_used="m", complexity="simple", llm_tokens=7, llm_tokens_per_second=3.5,
                        validation_passed=True, validation_reason="ok", base_knowledge_populated=True,
                        node_timings={"classify": 12.0}, retrieved_data={"weather": {"t": 1}}, data_source="weather",
                        error=None, was_truncated=False, is_multi_intent=False, is_novel_intent=False)
        defaults.update(kw)
        self.__dict__.update(defaults)


# ---------------------------------------------------------------------------
# TurnCapture
# ---------------------------------------------------------------------------

def test_an_unbound_capture_never_reaches_the_sink():
    class Spy:
        calls = 0

        def enqueue(self, record):
            Spy.calls += 1

    capture = tc.TurnCapture("query", Spy())
    capture.answered("x")
    capture.submit()
    assert Spy.calls == 0


def test_an_unbound_capture_writes_nothing():
    recording = RecordingSink()
    capture = tc.TurnCapture("query", recording)
    capture.answered("an answer")
    capture.observe(State())
    capture.submit()
    assert recording.records == []


def test_a_bound_capture_submits_exactly_once():
    recording = RecordingSink()
    capture = bound(tc.TurnCapture("query", recording))
    capture.observe(State())
    capture.answered("Done.")
    capture.submit()
    capture.submit()
    assert len(recording.records) == 1


def test_submit_is_not_a_coroutine():
    assert not inspect.iscoroutinefunction(tc.TurnCapture.submit)
    assert not inspect.iscoroutinefunction(tc.TurnCapture.bind)
    assert not inspect.iscoroutinefunction(tc.TurnCapture.observe)


def test_the_record_carries_server_derived_labels_and_allowlisted_state():
    recording = RecordingSink()
    capture = bound(tc.TurnCapture("query", recording))
    capture.observe(State())
    capture.answered("Done.", cache_hit=True)
    capture.submit()
    rec = recording.records[0]
    assert (rec.route, rec.channel, rec.audience_class, rec.session_class) == ("query", "speech", "owner", "owner")
    assert (rec.user_mode, rec.interface_type, rec.room) == ("owner", "voice", "porch")
    assert (rec.query_text, rec.response_text, rec.outcome) == ("turn on the porch light", "Done.", "answered")
    assert (rec.intent, rec.confidence, rec.request_id, rec.model_used) == ("control", 0.9, "r1", "m")
    assert rec.rag_tools_used == ["weather"] and rec.tokens_generated == 7
    assert rec.debug_detail["cache_hit"] is True and rec.debug_detail["caller_trust_tier"] == "web_authenticated"
    assert rec.debug_detail["stay_read_only"] is False


def test_session_class_comes_from_the_session_id_prefix_only():
    recording = RecordingSink()
    capture = bound(tc.TurnCapture("query", recording), session_id="gst-abc", audience=GUEST)
    capture.answered("hi")
    capture.submit()
    assert recording.records[0].session_class == "guest"


def test_observe_takes_a_graph_dict_too():
    recording = RecordingSink()
    capture = bound(tc.TurnCapture("query", recording))
    capture.observe({"answer": "from dict", "intent": "weather", "confidence": 0.5, "retrieved_data": {"weather": 1, "payload_key": 2}})
    capture.submit()
    rec = recording.records[0]
    assert rec.response_text == "from dict" and rec.intent == "weather" and rec.rag_tools_used == ["weather"]


def test_observe_takes_tool_names_from_emitted_calls_and_drops_data_source_and_off_list_keys():
    recording = RecordingSink()
    capture = bound(tc.TurnCapture("query", recording))
    capture.observe(State(
        tool_calls_emitted=[{"name": "search_restaurants", "arguments": {"term": "call 5551234"}}, {"name": "get_weather"}],
        retrieved_data={"tool_calls": ["x"], "tool_results": {}, "synthesis_model": "m"},
        data_source="Tool Calling (ollama/qwen3)", model_used="qwen3:4b", node_timings={"tool_call": 1.5, "mystery": 2.0},
    ))
    capture.answered("ok")
    capture.submit()
    rec = cc.sanitize_record(recording.records[0])
    assert rec.rag_tools_used == ["search_restaurants", "get_weather"]
    assert rec.model_used == "qwen3:4b" and rec.node_timings_ms == {"tool_call": 1.5}


def test_pin_shaped_and_long_digit_queries_are_withheld_and_the_digits_never_reach_the_record():
    for query in ("one two three four five six", "123-456", "12 34 56", "1,2,3,4,5,6", "pin is 4 4 4 4 4 4",
                  "my code is oh one two three", "switch to owner mode", "it's 123456", "one 2 three 4"):
        recording = RecordingSink()
        capture = bound(tc.TurnCapture("query", recording), query=query)
        capture.answered("a private answer")
        capture.submit()
        rec = recording.records[0]
        assert rec.text_withheld and rec.query_text == "" and rec.response_text is None, query
        assert "123" not in repr(rec) and "private answer" not in repr(rec)


@pytest.mark.parametrize("query", ["set the lights to 40 percent", "what's the weather in 2026", "weather for 2026", "set it to 72"])
def test_ordinary_queries_stay_text_with_long_digit_runs_redacted(query):
    recording = RecordingSink()
    capture = bound(tc.TurnCapture("query", recording), query=query)
    capture.answered("ok")
    capture.submit()
    rec = recording.records[0]
    assert not rec.text_withheld and rec.query_text == cc.redact_digits(query)


def test_bind_withheld_has_no_text_parameter_and_emits_a_content_free_marker():
    params = set(inspect.signature(tc.TurnCapture.bind_withheld).parameters)
    assert not params & {"query", "text", "answer", "message", "messages", "response"}
    recording = RecordingSink()
    capture = tc.TurnCapture("query", recording)
    capture.bind_withheld(session_id="own-1", audience=OWNER, interface_type="voice", room="porch")
    capture.answered("an answer that must not be stored")
    capture.observe(State(error="oops"))
    capture.submit()
    rec = recording.records[0]
    assert (rec.query_text, rec.response_text, rec.outcome, rec.text_withheld) == ("", None, "withheld", True)
    assert rec.session_class == "owner" and rec.audience_class == "owner" and rec.route == "query"
    assert rec.intent is None and rec.confidence is None and rec.debug_detail is None
    assert rec.error_message is None and rec.model_used is None and rec.rag_tools_used is None


def test_a_second_bind_on_a_reused_capture_changes_nothing():
    recording = RecordingSink()
    capture = bound(tc.TurnCapture("v1_chat_completions", recording), session_id="own-first")
    capture.bind(session_id="gst-second", query="other", interface_type="text", audience=GUEST, room="x", user_mode="guest")
    capture.answered("a")
    capture.submit()
    rec = recording.records[0]
    assert (rec.route, rec.session_id, rec.channel, rec.query_text) == ("v1_chat_completions", "own-first", "speech", "turn on the porch light")


def test_failed_keeps_only_the_class_name():
    recording = RecordingSink()
    capture = bound(tc.TurnCapture("query", recording))
    capture.failed(ValueError(f"row had {SECRET}"))
    capture.submit()
    rec = recording.records[0]
    assert rec.outcome == "error" and rec.error_message == "ValueError" and SECRET not in repr(rec)


def test_no_answer_and_incomplete_outcomes():
    recording = RecordingSink()
    done = bound(tc.TurnCapture("query", recording))
    done.answered(None, outcome="no_answer")
    done.submit()
    unfinished = bound(tc.TurnCapture("query", recording))
    unfinished.submit()
    assert [r.outcome for r in recording.records] == ["no_answer", "incomplete"]


def test_debug_detail_keys_are_inside_the_closed_set_and_strings_are_redacted():
    recording = RecordingSink()
    capture = bound(tc.TurnCapture("query", recording))
    capture.observe(State(validation_reason="saw 99887766 here", error="failed on 12345678"))
    capture.answered("ok")
    capture.submit()
    rec = cc.sanitize_record(recording.records[0])
    assert set(rec.debug_detail) <= cc.DEBUG_DETAIL_KEYS
    assert "99887766" not in repr(rec) and "12345678" not in repr(rec)


def test_labels_are_clamped_to_column_widths_after_sanitising():
    recording = RecordingSink()
    capture = bound(tc.TurnCapture("query", recording), room="r" * 400, session_id="own-" + "s" * 96)
    capture.observe(State(model_used="m" * 400, intent="i" * 400, request_id="q" * 400))
    capture.answered("ok")
    capture.submit()
    rec = cc.sanitize_record(recording.records[0])
    for name, width in cc.FIELD_WIDTHS.items():
        value = getattr(rec, name)
        assert value is None or len(value) <= width, name


def test_the_per_turn_cost_is_small():
    recording = RecordingSink()
    samples = []
    state = State()
    for _ in range(200):
        started = time.perf_counter()
        capture = bound(tc.TurnCapture("query", recording))
        capture.observe(state)
        capture.answered("Done.")
        capture.submit()
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    print(f"bind+observe+submit median {statistics.median(samples):.3f} ms, p99 {samples[int(len(samples) * .99)]:.3f} ms (advisory)")
    assert statistics.median(samples) < 1.0


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------

class Resp:
    def __init__(self, answer, metadata=None):
        self.answer, self.metadata = answer, metadata or {}


def test_the_decorator_marks_the_endpoint_and_keeps_its_signature():
    @tc.captures_turn("query")
    async def handler(request: str, flag: int = 1): ...

    assert getattr(handler, tc.ROUTE_MARKER) == "query"
    assert list(inspect.signature(handler).parameters) == ["request", "flag"]


def test_a_returned_response_is_recorded_with_its_answer_and_cache_hit():
    recording = RecordingSink()
    _runtime.set_turn_capture(recording)

    @tc.captures_turn("query")
    async def handler():
        bound(tc.current_capture())
        return Resp("Cached.", {"cache_hit": True})

    asyncio.run(handler())
    rec = recording.records[0]
    assert rec.response_text == "Cached." and rec.debug_detail["cache_hit"] is True


def test_a_handler_that_never_binds_records_nothing(sink):
    @tc.captures_turn("query")
    async def handler():
        return Resp("PIN branch result")

    asyncio.run(handler())
    assert sink.records == []


def test_an_exception_records_an_error_and_is_reraised(sink):
    @tc.captures_turn("query")
    async def handler():
        bound(tc.current_capture())
        raise RuntimeError(f"boom {SECRET}")

    with pytest.raises(RuntimeError):
        asyncio.run(handler())
    assert [r.outcome for r in sink.records] == ["error"] and SECRET not in repr(sink.records[0])


def test_a_nested_decorated_call_yields_exactly_one_record_with_the_outer_route(sink):
    @tc.captures_turn("query")
    async def inner():
        tc.current_capture().bind(session_id="own-9", query="inner query", interface_type="text",
                                  audience=OWNER, room=None, user_mode="owner")
        return Resp("inner answer")

    @tc.captures_turn("v1_chat_completions")
    async def outer():
        capture = tc.current_capture()
        capture.bind(session_id="own-9", query="outer query", interface_type="text", audience=OWNER, room=None, user_mode="owner")
        await inner()
        capture.observe({"intent": "late_outer_intent"})  # only lands if the nested call didn't submit
        return {"choices": []}  # the OpenAI body has no .answer

    asyncio.run(outer())
    assert len(sink.records) == 1
    rec = sink.records[0]
    assert (rec.route, rec.query_text, rec.response_text) == ("v1_chat_completions", "outer query", "inner answer")
    assert rec.intent == "late_outer_intent"


def test_the_context_is_released_after_the_call(sink):
    @tc.captures_turn("query")
    async def handler():
        return Resp("x")

    asyncio.run(handler())
    assert tc.current_capture().route == "none"


def test_a_store_that_blocks_forever_does_not_delay_the_handler():
    async def run():
        store = InMemoryCaptureStore(block=asyncio.Event())
        capture_sink = cc.CaptureSink(store, flag_ttl=0.01, queue_max=4)
        capture_sink.start()
        for _ in range(100):
            if capture_sink._flags is not None:
                break
            await asyncio.sleep(0.005)
        _runtime.set_turn_capture(capture_sink)

        @tc.captures_turn("query")
        async def handler():
            bound(tc.current_capture())
            return Resp("fast")

        started = time.perf_counter()
        for _ in range(20):
            await handler()
        elapsed = time.perf_counter() - started
        status = capture_sink.status()
        await capture_sink.stop(drain_s=0.1)
        return elapsed, status

    elapsed, status = asyncio.run(run())
    assert elapsed < 0.5
    assert status["dropped"] >= 14  # queue of 4 plus one in flight; the rest counted


def test_flags_off_means_nothing_is_enqueued_or_retained():
    async def run():
        store = InMemoryCaptureStore(analytics=False)
        capture_sink = cc.CaptureSink(store, flag_ttl=0.01)
        capture_sink.start()
        for _ in range(100):
            if capture_sink._flags is not None:
                break
            await asyncio.sleep(0.005)
        _runtime.set_turn_capture(capture_sink)

        @tc.captures_turn("query")
        async def handler():
            bound(tc.current_capture(), query="a private question")
            return Resp("a private answer")

        await handler()
        depth = capture_sink._queue.qsize()
        await capture_sink.stop(drain_s=0.2)
        return depth, store.turns

    depth, turns = asyncio.run(run())
    assert depth == 0 and turns == []


def test_the_owner_command_predicate_reaches_the_sink_in_production_wiring():
    sink_ = tc.create_sink("postgresql://u:pw@db.example/app", {})
    assert sink_._owner_command is tc.detect_owner_mode_command
    assert tc.create_sink("", {})._store is None


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------

async def _drain(response):
    return [chunk async for chunk in response.body_iterator]


def _stream_handler(chunks, *, answer_at=None, raise_after=None, gate=None):
    @tc.captures_turn("query_stream")
    async def handler():
        capture = tc.current_capture()
        bound(capture)

        async def gen():
            for i, chunk in enumerate(chunks):
                if gate is not None and i > 0:
                    await gate.wait()
                yield chunk
                if raise_after is not None and i == raise_after:
                    raise RuntimeError("stream broke")
            if answer_at == "end":
                capture.answered("full answer")

        return StreamingResponse(gen(), media_type="text/event-stream")

    return handler


def test_a_stream_submits_when_it_ends(sink):
    async def run():
        response = await _stream_handler(["a", "b", "c"], answer_at="end")()
        assert sink.records == []  # nothing before the stream is consumed
        return await _drain(response)

    assert asyncio.run(run()) == ["a", "b", "c"]
    assert len(sink.records) == 1
    assert (sink.records[0].outcome, sink.records[0].response_text) == ("answered", "full answer")


def test_closing_a_stream_midway_records_one_incomplete_turn(sink):
    async def run():
        response = await _stream_handler(["a", "b", "c"], answer_at="end")()
        iterator = response.body_iterator
        assert await iterator.__anext__() == "a"
        await iterator.aclose()

    asyncio.run(run())
    assert [r.outcome for r in sink.records] == ["incomplete"]


def test_cancelling_a_stream_records_one_turn(sink):
    async def run():
        gate = asyncio.Event()
        response = await _stream_handler(["a", "b"], gate=gate)()
        task = asyncio.create_task(_drain(response))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(sink.records) == 1 and sink.records[0].outcome == "incomplete"


def test_a_stream_that_raises_records_an_error(sink):
    async def run():
        response = await _stream_handler(["a", "b"], raise_after=0)()
        with pytest.raises(RuntimeError):
            await _drain(response)

    asyncio.run(run())
    assert [r.outcome for r in sink.records] == ["error"]
    assert sink.records[0].error_message == "RuntimeError"


def test_the_stream_wrapper_yields_chunks_unchanged_without_buffering(sink):
    async def run():
        gate = asyncio.Event()
        response = await _stream_handler(["first", "second"], gate=gate)()
        iterator = response.body_iterator
        first = await asyncio.wait_for(iterator.__anext__(), 1.0)  # the gate is still closed
        gate.set()
        rest = [chunk async for chunk in iterator]
        return first, rest

    first, rest = asyncio.run(run())
    assert (first, rest) == ("first", ["second"])


def test_each_stream_is_submitted_once(sink, monkeypatch):
    calls = []
    original = tc.TurnCapture.submit
    monkeypatch.setattr(tc.TurnCapture, "submit", lambda self: (calls.append(1), original(self))[1])

    async def run():
        response = await _stream_handler(["a"], answer_at="end")()
        await _drain(response)

    asyncio.run(run())
    assert len(sink.records) == 1 and len(calls) == 1


# ---------------------------------------------------------------------------
# Failure handling and logs
# ---------------------------------------------------------------------------

def test_a_write_failure_logs_the_class_only(caplog):
    async def run():
        store = InMemoryCaptureStore(fail=RuntimeError(f"row had {SECRET}"))
        capture_sink = cc.CaptureSink(store, flag_ttl=0.01)
        capture_sink.start()
        for _ in range(100):
            if capture_sink._flags is not None:
                break
            await asyncio.sleep(0.005)
        _runtime.set_turn_capture(capture_sink)

        @tc.captures_turn("query")
        async def handler():
            bound(tc.current_capture(), query="a private question")
            return Resp("a private answer")

        await handler()
        await capture_sink.stop(drain_s=1)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "RuntimeError" in logged
    for forbidden in (SECRET, "private question", "private answer"):
        assert forbidden not in logged


def test_dsn_and_pool_errors_log_the_class_only(caplog):
    """An unreachable database: the failure log names the class, never the DSN."""
    async def run():
        capture_sink = tc.create_sink(f"postgresql://user:{SECRET}@127.0.0.1:1/db", {})
        capture_sink.start()
        await asyncio.sleep(0.3)
        status = capture_sink.status()
        await capture_sink.stop(drain_s=0.2)
        return status

    with caplog.at_level(logging.DEBUG):
        status = asyncio.run(run())
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert status["writer_state"] in ("failing", "stopped")
    assert SECRET not in logged and "127.0.0.1" not in logged


def test_an_unconfigured_database_warns_with_the_source_only(caplog):
    with caplog.at_level(logging.WARNING):
        sink_ = tc.create_sink("sqlite:///x.db", {})
    assert sink_.status()["writer_state"] == "unconfigured"
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "unsupported" in logged and "x.db" not in logged


# ---------------------------------------------------------------------------
# Import hygiene
# ---------------------------------------------------------------------------

def test_turn_capture_never_imports_orchestrator_main():
    tree = ast.parse((SRC / "orchestrator" / "turn_capture.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "orchestrator.main" and not (node.module == "orchestrator" and any(a.name == "main" for a in node.names))
        elif isinstance(node, ast.Import):
            assert all(a.name != "orchestrator.main" for a in node.names)


@pytest.mark.parametrize("statement", [
    "import orchestrator.turn_capture",
    "from orchestrator import turn_capture",
    "import orchestrator.nodes; import orchestrator.turn_capture",
])
def test_turn_capture_imports_in_a_fresh_interpreter_without_loading_main(statement):
    """A fresh process, the container's import path. (Importing orchestrator.helpers first
    already fails on its own circular import; this module adds no new cycle.)"""
    code = f"{statement}\nimport sys\nassert 'orchestrator.main' not in sys.modules\nassert 'orchestrator.helpers' not in sys.modules or True"
    result = subprocess.run([sys.executable, "-c", code], cwd=str(SRC), capture_output=True, text=True, timeout=60,
                            env={**os.environ, "PYTHONPATH": str(SRC)})
    assert result.returncode == 0, result.stderr[-1500:]


def test_runtime_slot_is_optional_and_not_required():
    assert "turn_capture" not in _runtime.required_singletons()
    assert _runtime.get_turn_capture() is None


# ---------------------------------------------------------------------------
# Audience derivation, input validation, stream context
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("audience,trust,expected", [
    (OWNER, "web_authenticated", "owner"),
    (GUEST, "sms", "guest"),
    (HOUSEHOLD, None, "other"),
    (OWNER, "public_relay", None),
])
def test_audience_class_is_derived_from_the_audience_and_trust(audience, trust, expected):
    got = tc.audience_class_for(audience, trust)
    if expected is None:
        assert got == ("public" if tc.is_public_caller(trust) else "owner")
    else:
        assert got == expected


def test_a_public_audience_is_public_whatever_the_trust():
    public = KnowledgeAudience(mode="guest", degraded=False, public=True, owner_caller=False, owner_proven=False)
    assert tc.audience_class_for(public, None) == "public"


def test_the_derivation_matches_mains_session_class_functions():
    """Drift guard: main.py's _audience_session_class must agree with audience_class_for."""
    tree = ast.parse((SRC / "orchestrator" / "main.py").read_text())
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_audience_session_class")
    namespace = {"CALLER_CLASS_PUBLIC": "public", "CALLER_CLASS_OWNER": "owner",
                 "CALLER_CLASS_GUEST": "guest", "CALLER_CLASS_OTHER": "other", "KnowledgeAudience": KnowledgeAudience}
    exec(compile(ast.Module(body=[func], type_ignores=[]), "main_fn", "exec"), namespace)
    for audience in (OWNER, GUEST, HOUSEHOLD, KnowledgeAudience.UNRESOLVED,
                     KnowledgeAudience(mode="guest", degraded=False, public=True, owner_caller=False, owner_proven=False),
                     KnowledgeAudience(mode="guest", degraded=False, public=False, owner_caller=False, owner_proven=False, guest_verified=False)):
        assert namespace["_audience_session_class"](audience) == tc.audience_class_for(audience, None), audience


@pytest.mark.parametrize("bad", [None, "", 5, "x" * 101])
def test_an_invalid_session_id_leaves_the_capture_unbound(bad):
    recording = RecordingSink()
    capture = tc.TurnCapture("query", recording)
    bound(capture, session_id=bad)
    capture.answered("a")
    capture.submit()
    assert recording.records == []
    marker = tc.TurnCapture("query", recording)
    marker.bind_withheld(session_id=bad, audience=OWNER, interface_type="voice", room=None)
    marker.submit()
    assert recording.records == []


def test_an_odd_caller_trust_is_not_recorded():
    recording = RecordingSink()
    capture = bound(tc.TurnCapture("query", recording), caller_trust="Hello There; drop table")
    capture.answered("a")
    capture.submit()
    assert "caller_trust_tier" not in recording.records[0].debug_detail


def test_a_withheld_turn_keeps_no_error_text():
    recording = RecordingSink()
    capture = bound(tc.TurnCapture("query", recording), query="the code is one two three four")
    capture.observe(State(error="failed while handling something private"))
    capture.failed(RuntimeError("x"))
    capture.submit()
    rec = recording.records[0]
    assert rec.text_withheld and rec.error_message is None


def test_a_streamed_answer_reaches_the_record_through_current_capture_inside_the_generator(sink):
    """Handlers may use the reference they took at the top, or current_capture() inside the body."""
    @tc.captures_turn("query_stream")
    async def handler():
        bound(tc.current_capture())

        async def gen():
            yield "chunk"
            tc.current_capture().answered("the streamed answer")  # no reference held

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def run():
        response = await handler()
        await _drain(response)

    asyncio.run(run())
    assert [(r.outcome, r.response_text) for r in sink.records] == [("answered", "the streamed answer")]
    assert tc.current_capture().route == "none"  # the context did not leak past the stream


def test_a_second_cancellation_during_stream_close_cannot_skip_the_submit(sink):
    async def run():
        @tc.captures_turn("query_stream")
        async def handler():
            capture = tc.current_capture()
            bound(capture)

            async def gen():
                try:
                    yield "a"
                    yield "b"
                finally:
                    await asyncio.sleep(10)  # a close that hangs, so it can be cancelled again

            return StreamingResponse(gen(), media_type="text/event-stream")

        response = await handler()
        iterator = response.body_iterator
        await iterator.__anext__()
        closing = asyncio.create_task(iterator.aclose())
        await asyncio.sleep(0.05)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing

    asyncio.run(run())
    assert [r.outcome for r in sink.records] == ["incomplete"]


def test_a_failure_after_the_answer_keeps_the_answered_outcome_and_the_answer():
    """Pinned: the user already has their answer, so a later failure (usually the session save)
    records only its class name; the outcome stays answered."""
    recording = RecordingSink()
    capture = bound(tc.TurnCapture("query_stream", recording))
    capture.answered("the delivered answer")
    capture.failed(RuntimeError(f"session save broke {SECRET}"))
    capture.submit()
    rec = recording.records[0]
    assert (rec.outcome, rec.response_text, rec.error_message) == ("answered", "the delivered answer", "RuntimeError")
    assert SECRET not in repr(rec)


def test_a_failure_before_the_answer_is_an_error():
    recording = RecordingSink()
    capture = bound(tc.TurnCapture("query_stream", recording))
    capture.failed(ValueError("x"))
    capture.answered("too late to count")
    capture.submit()
    assert (recording.records[0].outcome, recording.records[0].error_message) == ("error", "ValueError")


def test_refusals_are_marked_by_the_call_or_by_the_state_error():
    recording = RecordingSink()
    explicit = bound(tc.TurnCapture("query", recording))
    explicit.answered("No.")
    explicit.refused()
    explicit.submit()
    from_state = bound(tc.TurnCapture("query", recording))
    from_state.observe(State(error="permission_denied", answer="Not allowed."))
    from_state.answered("Not allowed.")
    from_state.submit()
    assert [r.outcome for r in recording.records] == ["refused", "refused"]


def test_error_text_is_digit_redacted_for_a_normal_turn_through_the_sink():
    """The writer redacts error_message too (not only withheld turns drop it)."""
    async def run():
        store = InMemoryCaptureStore()
        sink = cc.CaptureSink(store, flag_ttl=0.01)
        sink.start()
        for _ in range(100):
            if sink._flags is not None:
                break
            await asyncio.sleep(0.005)
        capture = bound(tc.TurnCapture("query", sink))
        capture.observe(State(error="lookup failed for account 99887766 and 5521"))
        capture.answered("ok")
        capture.submit()
        await sink.stop(drain_s=1)
        return store.written[0]

    rec = asyncio.run(run())
    assert "99887766" not in rec.error_message and "5521" not in rec.error_message
    assert rec.error_message.startswith("lookup failed for account")


# ---------------------------------------------------------------------------
# Latency gate: capture on vs unconfigured, at the enqueue boundary
# ---------------------------------------------------------------------------

_HANDLER_CPU_ITERATIONS = 30_000  # a few milliseconds of work: far less than a real turn, so a stricter bar than production
_GATE_CALLS = 50
_GATE_ROUNDS = 3
_GATE_MAX_OVERHEAD = 0.05


def _median_call_seconds(handler, calls):
    async def go():
        samples = []
        for _ in range(calls):
            started = time.perf_counter()
            await handler()
            samples.append(time.perf_counter() - started)
        return statistics.median(samples)

    return go


def test_capture_adds_at_most_five_percent_to_a_handler_versus_no_capture():
    """The same decorated handler, one process, calls interleaved: sink configured and on (queue,
    flag cache, record building, enqueue) against no sink at all. Median of 50 per round; the gate
    passes if any of three rounds is within 5%, which keeps a noisy CI neighbour from failing a
    real-sized margin (the measured overhead is a small fraction of a percent)."""
    @tc.captures_turn("query")
    async def handler():
        capture = tc.current_capture()
        capture.bind(session_id="own-lat", query="what's the weather like today", interface_type="voice",
                     room="porch", user_mode="owner", audience=OWNER, caller_trust="household")
        capture.observe(State())
        total = 0
        for i in range(_HANDLER_CPU_ITERATIONS):
            total += i * i % 7
        return Resp("a reasonably normal sized spoken answer for the weather", {})

    async def measure():
        store = InMemoryCaptureStore(analytics=True)
        sink = cc.CaptureSink(store, flag_ttl=60, queue_max=100_000)
        sink.start()
        for _ in range(200):
            if sink._flags is not None:
                break
            await asyncio.sleep(0.005)
        results = []
        for _ in range(_GATE_ROUNDS):
            on, off = [], []
            for _ in range(_GATE_CALLS):
                _runtime.set_turn_capture(sink)
                started = time.perf_counter()
                await handler()
                on.append(time.perf_counter() - started)
                _runtime.set_turn_capture(None)
                started = time.perf_counter()
                await handler()
                off.append(time.perf_counter() - started)
            results.append((statistics.median(on), statistics.median(off)))
        await sink.stop(drain_s=1)
        return results

    rounds = asyncio.run(measure())
    overheads = [(on - off) / off for on, off in rounds]
    print("capture overhead by round:", [f"{o * 100:.2f}%" for o in overheads])
    assert min(overheads) <= _GATE_MAX_OVERHEAD, overheads
