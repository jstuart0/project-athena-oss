"""Every orchestrator answer route records exactly one turn, black-box.

Scenarios run through the real FastAPI app (httpx's ASGI transport, and a raw
ASGI driver for client disconnects), a real ``CaptureSink`` and an
``InMemoryCaptureStore`` that follows the writer statement's rules. The
statement itself is exercised on Postgres in admin/backend/tests/integration/.
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest

from . import _public_audience_harness as h
from ._capture_fakes import InMemoryCaptureStore
from orchestrator import turn_capture as tc
from orchestrator.session_keys import id_class
from shared import conversation_capture as cc
from shared.knowledge_tiers import KnowledgeAudience

QUERY = "what's the weather in Anytown today"
RAW = "Winds 25mph, gusts 15 km/h."
SPOKEN = "Winds 25 miles per hour, gusts 15 kilometers per hour."
PIN_MESSAGE = "Owner mode on."
PIN_QUERY = "switch to owner mode pin 4 8 1 5 1 6"
FOLLOW_UP = "yes please that one"

STREAM_ROUTES = ["/query/stream", "/query/stream/v2", "/v1/chat/completions"]
ALL_ROUTES = ["/query", "/query/stream", "/query/stream/v2", "/v1/chat/completions:stream", "/v1/chat/completions"]
ROUTE_LABEL = {"/query": "query", "/query/stream": "query_stream", "/query/stream/v2": "query_stream_v2",
               "/v1/chat/completions:stream": "v1_chat_completions", "/v1/chat/completions": "v1_chat_completions"}


class Graph:
    def __init__(self, answer=RAW, error=None):
        self.answer, self.error, self.calls = answer, error, 0

    async def ainvoke(self, state):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"intent": h.IntentCategory.WEATHER, "answer": self.answer, "confidence": 1.0, "citations": [],
                "request_id": "r1", "node_timings": {"classify": 3.0}, "validation_passed": True,
                "retrieved_data": {"weather": {}}, "data_source": "weather", "model_used": "m"}


class SessionCache:
    def __init__(self):
        self.client = SimpleNamespace(delete=mock.AsyncMock(), get=mock.AsyncMock(return_value=None), setex=mock.AsyncMock())


class MemoryCache:
    def __init__(self):
        self.data, self.sets = {}, 0

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ttl=None):
        self.sets += 1
        self.data[key] = value


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


@pytest.fixture
def rt(monkeypatch):
    """Runtime wiring shared by every scenario; returns the pieces tests tweak."""
    h.patch_conversation_config(monkeypatch)
    monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: True)
    h.install_mode_client(server_mode="owner")
    monkeypatch.setattr(h.main, "get_admin_client", lambda: h.fake_admin_client())
    h._runtime.set_cache_client(SessionCache())
    graph = Graph()
    monkeypatch.setattr(h.main, "orchestrator_graph", graph)

    async def run_stream(state):
        state.intent = h.IntentCategory.WEATHER
        state.answer = RAW
        return state

    monkeypatch.setattr(h.main, "run_orchestrator_for_streaming", run_stream)

    def pin_for(match="owner mode"):
        async def utterance(query, caller_trust, room):
            if match in (query or ""):
                return SimpleNamespace(message=PIN_MESSAGE, success=True, override_data=None, refused_reason=None)
            return None

        monkeypatch.setattr(h.main, "handle_owner_mode_utterance", utterance)

    return SimpleNamespace(graph=graph, pin_for=pin_for, monkeypatch=monkeypatch)


def request_for(route, query=QUERY, *, interface_type="voice", session_id=None, mode=None, messages=None):
    """(path, json body) for one scenario route."""
    if route.startswith("/v1/chat/completions"):
        body = {"model": "m", "stream": route.endswith(":stream"),
                "messages": messages or [{"role": "user", "content": query}],
                "extra_body": {"interface_type": interface_type, "room": "porch"}}
        if session_id:
            body["session_id"] = session_id
        return "/v1/chat/completions", body
    body = {"query": query, "caller_trust": "household", "interface_type": interface_type, "room": "porch",
            "skip_semantic_cache": True}
    if session_id:
        body["session_id"] = session_id
    if mode:
        body["mode"] = mode
    return route, body


async def started_sink(store):
    sink = cc.CaptureSink(store, flag_ttl=0.01, owner_command=tc.detect_owner_mode_command)
    sink.start()
    for _ in range(200):
        if sink._flags is not None:
            break
        await asyncio.sleep(0.005)
    h._runtime.set_turn_capture(sink)
    return sink


def run(scenario, *, analytics=True, debug=False):
    """Run ``scenario(client)`` with a live sink; returns the store."""
    async def go():
        store = InMemoryCaptureStore(analytics=analytics, debug=debug)
        sink = await started_sink(store)
        transport = httpx.ASGITransport(app=h.main.app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=h.service_headers()) as client:
                await scenario(client)
        finally:
            await sink.stop(drain_s=2)
        return store

    return asyncio.run(go())


async def post(client, route, **kw):
    path, body = request_for(route, **kw)
    return await client.post(path, json=body)


def one(store):
    assert len(store.written) == 1, [r.query_text for r in store.written]
    return store.written[0]


# ---------------------------------------------------------------------------
# One record per answered turn, on every route
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", ALL_ROUTES)
def test_each_route_records_exactly_one_turn_with_server_derived_labels(rt, route):
    async def scenario(client):
        resp = await post(client, route)
        assert resp.status_code == 200

    store = run(scenario)
    rec = one(store)
    assert rec.route == ROUTE_LABEL[route]
    assert (rec.channel, rec.interface_type) == ("speech", "voice")
    assert rec.response_text == RAW, "the raw answer, not the spoken rendering"
    assert rec.query_text == QUERY
    assert rec.user_mode == "owner"
    assert (rec.audience_class, rec.session_class) == ("other", "other")
    assert rec.session_class == id_class(rec.session_id)
    assert rec.outcome == "answered"
    assert rec.room == "porch"


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_analytics_off_means_zero_records(rt, route):
    async def scenario(client):
        assert (await post(client, route)).status_code == 200

    store = run(scenario, analytics=False)
    assert store.turns == [] and store.written == []


def test_nothing_in_capture_reads_the_debug_env_var():
    """ATHENA_DEBUG_MODE only means log-file verbosity. Capture is decided by the database flags
    inside the writer statement; no capture code reads the variable (the drift test pins main.py)."""
    for path in (h.ORCH_DIR / "main.py", h.ORCH_DIR / "turn_capture.py", h.REPO_ROOT / "src" / "shared" / "conversation_capture.py"):
        assert "ATHENA_DEBUG_MODE" not in path.read_text(), path.name


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_debug_detail_is_stored_only_while_the_debug_flag_is_on(rt, route):
    async def scenario(client):
        await post(client, route)

    off = run(scenario, debug=False)
    on = run(scenario, debug=True)
    assert off.turns[0]["debug_detail"] is None and off.turns[0]["source"] == "analytics"
    assert on.turns[0]["debug_detail"] and on.turns[0]["source"] == "debug"


# ---------------------------------------------------------------------------
# /query: every exit
# ---------------------------------------------------------------------------

def test_the_tool_creation_exit_is_recorded(rt):
    rt.monkeypatch.setattr(h.main, "detect_tool_creation_intent", lambda q: True)
    rt.monkeypatch.setattr(h.main, "handle_tool_creation_request",
                           mock.AsyncMock(return_value={"answer": "Tool drafted.", "success": True}))

    async def scenario(client):
        assert (await post(client, "/query")).status_code == 200

    rec = one(run(scenario))
    assert (rec.route, rec.response_text, rec.outcome) == ("query", "Tool drafted.", "answered")
    assert rt.graph.calls == 0


def test_the_memory_forget_exit_is_recorded(rt):
    manager = SimpleNamespace(
        should_forget_memory=lambda q: True, extract_forget_content=lambda q: "the garage code",
        delete_memory_by_content=mock.AsyncMock(return_value={"deleted": 1}),
    )
    rt.monkeypatch.setattr(h.main, "get_memory_manager", mock.AsyncMock(return_value=manager))
    rt.monkeypatch.setattr(h.main, "detect_tool_creation_intent", lambda q: False)

    async def scenario(client):
        assert (await post(client, "/query")).status_code == 200

    rec = one(run(scenario))
    assert rec.response_text.startswith("Done! I've forgotten 1 memory") and rec.route == "query"
    assert rt.graph.calls == 0


def test_the_post_graph_refusal_exit_is_recorded(rt):
    rt.monkeypatch.setattr(h.main, "post_graph_intent_refusal", lambda intent, error, permissions: "I can't do that here.")

    async def scenario(client):
        assert (await post(client, "/query")).status_code == 200

    rec = one(run(scenario))
    assert rec.response_text == "I can't do that here." and rec.outcome == "refused"


@pytest.mark.parametrize("route", ["/query/stream", "/query/stream/v2", "/v1/chat/completions:stream"])
def test_a_refusal_on_the_other_routes_is_recorded_as_refused(rt, route):
    rt.monkeypatch.setattr(h.main, "post_graph_intent_refusal", lambda intent, error, permissions: "I can't do that here.")

    async def scenario(client):
        assert (await post(client, route)).status_code == 200

    rec = one(run(scenario))
    assert rec.response_text == "I can't do that here." and rec.outcome == "refused"


def test_a_graph_level_permission_denial_is_recorded_as_refused(rt):
    original = rt.graph.ainvoke

    async def denied(state):
        result = await original(state)
        return {**result, "error": "permission_denied", "answer": "Not allowed."}

    rt.graph.ainvoke = denied

    async def scenario(client):
        assert (await post(client, "/query")).status_code == 200

    rec = one(run(scenario))
    assert rec.outcome == "refused" and rec.response_text == "Not allowed."


def test_a_cache_hit_is_recorded_with_the_cache_flag(rt):
    import orchestrator.semantic_cache as semantic_cache

    cache = MemoryCache()
    rt.monkeypatch.setattr(semantic_cache, "get_cache_client", lambda: cache)

    async def scenario(client):
        path, body = request_for("/query")
        body.pop("skip_semantic_cache")
        assert (await client.post(path, json=body)).status_code == 200
        for _ in range(200):
            if cache.sets:
                break
            await asyncio.sleep(0.01)
        assert (await client.post(path, json=body)).status_code == 200

    store = run(scenario, debug=True)
    assert rt.graph.calls == 1, "the second request was a cache hit"
    assert len(store.written) == 2
    assert store.written[0].debug_detail["cache_hit"] is False
    assert store.written[1].debug_detail["cache_hit"] is True
    assert store.written[1].response_text == RAW


# ---------------------------------------------------------------------------
# Errors and no-answer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", ["/query", "/query/stream/v2"])
def test_a_graph_that_raises_records_an_error_with_the_class_name_only(rt, route):
    rt.graph.error = RuntimeError("secret internal detail 55512")

    async def scenario(client):
        await post(client, route)

    rec = one(run(scenario))
    assert rec.outcome == "error" and rec.error_message == "RuntimeError"
    assert "secret internal detail" not in repr(rec)


@pytest.mark.parametrize("route", ["/query/stream", "/v1/chat/completions:stream"])
def test_a_stream_pipeline_that_raises_records_an_error(rt, route):
    async def broken(state):
        raise ValueError("secret internal detail")

    rt.monkeypatch.setattr(h.main, "run_orchestrator_for_streaming", broken)

    async def scenario(client):
        try:
            await post(client, route)
        except Exception:
            pass

    rec = one(run(scenario))
    assert rec.outcome == "error" and rec.error_message == "ValueError"


def test_v2_with_no_answer_records_no_answer(rt):
    rt.graph.answer = ""

    async def scenario(client):
        await post(client, "/query/stream/v2")

    rec = one(run(scenario))
    assert rec.outcome == "no_answer" and rec.response_text is None


# ---------------------------------------------------------------------------
# Owner-PIN utterances
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", ALL_ROUTES)
def test_a_pin_utterance_stores_one_content_free_marker(rt, route):
    rt.pin_for()

    async def scenario(client):
        assert (await post(client, route, query=PIN_QUERY)).status_code == 200

    store = run(scenario)
    rec = one(store)
    assert (rec.text_withheld, rec.outcome, rec.query_text, rec.response_text, rec.debug_detail) == (True, "withheld", "", None, None)
    assert rec.intent is None and rec.confidence is None and rec.error_message is None
    assert rec.session_class == id_class(rec.session_id) and rec.route == ROUTE_LABEL[route]
    assert "4 8 1 5 1 6" not in repr(store.turns) and "481516" not in repr(store.turns)
    assert PIN_MESSAGE not in repr(store.turns)


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_a_pin_utterance_with_analytics_off_stores_nothing(rt, route):
    rt.pin_for()

    async def scenario(client):
        await post(client, route, query=PIN_QUERY)

    assert run(scenario, analytics=False).turns == []


@pytest.mark.parametrize("route", ALL_ROUTES)
def test_the_turn_after_a_pin_utterance_is_withheld_in_the_same_conversation(rt, route):
    rt.pin_for()

    async def scenario(client):
        first = await post(client, route, query=PIN_QUERY, session_id="explicit-sticky")
        assert first.status_code == 200
        second = await post(client, route, query=FOLLOW_UP, session_id="explicit-sticky")
        assert second.status_code == 200

    store = run(scenario)
    assert len(store.turns) == 2
    assert [t["text_withheld"] for t in store.turns] == [True, True]
    assert len(store.conversations) == 1
    assert all(t["query_text"] == "" and t["response_text"] is None for t in store.turns)


# ---------------------------------------------------------------------------
# /v1: only the final user message is bound
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", ["/v1/chat/completions", "/v1/chat/completions:stream"])
def test_a_pin_in_an_earlier_history_message_never_reaches_a_record(rt, route):
    messages = [
        {"role": "user", "content": "my pin is 7 3 9 2 0 4"},
        {"role": "assistant", "content": "I can't take that here."},
        {"role": "user", "content": QUERY},
    ]

    async def scenario(client):
        assert (await post(client, route, messages=messages)).status_code == 200

    store = run(scenario)
    rec = one(store)
    assert rec.query_text == QUERY
    assert "739204" not in repr(store.turns) and "7 3 9 2 0 4" not in repr(store.turns)


def test_user_mode_is_the_servers_even_when_the_request_claims_owner(rt):
    h.install_mode_client(server_mode="guest")

    async def scenario(client):
        assert (await post(client, "/query", mode="owner")).status_code == 200

    rec = one(run(scenario))
    assert rec.user_mode == "guest" and rec.audience_class == "guest"


# ---------------------------------------------------------------------------
# Owner and guest stay in separate conversations
# ---------------------------------------------------------------------------

def test_guest_and_household_turns_land_in_separate_conversations(rt):
    async def scenario(client):
        h.install_mode_client(server_mode="guest")
        assert (await post(client, "/query")).status_code == 200
        h.install_mode_client(server_mode="owner")
        assert (await post(client, "/query")).status_code == 200

    store = run(scenario)
    assert len(store.written) == 2
    guest, household = store.written
    assert (guest.session_class, guest.audience_class) == ("guest", "guest")
    assert guest.session_id.startswith("gst-")
    assert (household.session_class, household.audience_class) == ("other", "other")
    assert len(store.conversations) == 2


# ---------------------------------------------------------------------------
# Disconnects through the real ASGI stack
# ---------------------------------------------------------------------------

async def drive(route, *, disconnect_after_first_chunk=False):
    """Call the app directly so the client can walk away mid-stream."""
    path, body = request_for(route)
    payload = json.dumps(body).encode()
    disconnect, state, sent = asyncio.Event(), {"sent": False}, []

    async def receive():
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if disconnect_after_first_chunk and message["type"] == "http.response.body" and message.get("body"):
            disconnect.set()

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "path": path,
             "raw_path": path.encode(), "query_string": b"", "scheme": "http", "root_path": "", "server": ("t", 80),
             "client": ("127.0.0.1", 1234),
             "headers": [(b"content-type", b"application/json"), (b"host", b"t"),
                         (b"x-service-key", h.service_headers()["X-Service-Key"].encode())]}
    try:
        await asyncio.wait_for(h.main.app(scope, receive, send), 10)
    except BaseException as exc:  # a cancelled or failed stream is the point of these tests
        sent.append({"type": "exception", "name": type(exc).__name__})
    return sent


def run_driven(route, **kw):
    async def go():
        store = InMemoryCaptureStore()
        sink = await started_sink(store)
        spy = {"submits": 0}
        original = tc.TurnCapture.submit

        def counting(self):
            spy["submits"] += 1
            return original(self)

        tc.TurnCapture.submit = counting
        try:
            await drive(route, **kw)
        finally:
            tc.TurnCapture.submit = original
            await sink.stop(drain_s=2)
        return store, spy

    return asyncio.run(go())


@pytest.mark.parametrize("route", ["/query/stream", "/query/stream/v2", "/v1/chat/completions:stream"])
def test_a_normal_stream_finish_submits_once(rt, route):
    store, spy = run_driven(route)
    assert len(store.written) == 1 and store.written[0].outcome == "answered"
    assert spy["submits"] == 1


@pytest.mark.parametrize("route", ["/query/stream", "/query/stream/v2", "/v1/chat/completions:stream"])
def test_a_client_that_leaves_after_the_first_chunk_records_one_incomplete_turn(rt, route):
    if route != "/v1/chat/completions:stream":
        # these streams send a status event before the pipeline runs; hold the pipeline
        # so the client is certain to be gone while it is still working
        gate = asyncio.Event()

        async def slow(state):
            await gate.wait()
            return state

        rt.monkeypatch.setattr(h.main, "run_orchestrator_for_streaming", slow)
        rt.graph.ainvoke = lambda state: slow(state)
    store, spy = run_driven(route, disconnect_after_first_chunk=True)
    assert len(store.written) == 1 and spy["submits"] == 1
    assert store.written[0].outcome == "incomplete"


@pytest.mark.parametrize("route", ["/query/stream", "/query/stream/v2", "/v1/chat/completions:stream"])
def test_a_cancelled_pipeline_records_one_turn(rt, route):
    async def cancelled(state):
        raise asyncio.CancelledError()

    rt.monkeypatch.setattr(h.main, "run_orchestrator_for_streaming", cancelled)
    rt.graph.ainvoke = cancelled
    store, spy = run_driven(route)
    assert len(store.written) == 1 and spy["submits"] == 1
    assert store.written[0].outcome == "incomplete"


@pytest.mark.parametrize("route", ["/query/stream", "/query/stream/v2", "/v1/chat/completions:stream"])
def test_a_pipeline_that_fails_midway_records_one_error(rt, route):
    async def broken(state):
        raise RuntimeError("secret failure")

    rt.monkeypatch.setattr(h.main, "run_orchestrator_for_streaming", broken)
    rt.graph.ainvoke = broken
    store, spy = run_driven(route)
    assert len(store.written) == 1 and spy["submits"] == 1
    assert store.written[0].outcome == "error" and store.written[0].error_message == "RuntimeError"


# ---------------------------------------------------------------------------
# Token-streamed answers (no precomputed handler answer)
# ---------------------------------------------------------------------------

class TokenLLM:
    def __init__(self, tokens):
        self.tokens = tokens

    async def generate_stream(self, model=None, prompt=None, system_prompt=None, **kwargs):
        for token in self.tokens:
            yield {"token": token}
        yield {"token": "", "done": True}


@pytest.mark.parametrize("route", ["/query/stream", "/v1/chat/completions:stream"])
def test_a_token_streamed_answer_is_recorded_raw(rt, route):
    async def no_handler_answer(state):
        state.intent = h.IntentCategory.WEATHER
        state.answer = ""
        return state

    async def prompt(state):
        return ("prompt", "model", "system")

    rt.monkeypatch.setattr(h.main, "run_orchestrator_for_streaming", no_handler_answer)
    rt.monkeypatch.setattr(h.main, "build_synthesis_prompt_for_streaming", prompt)
    rt.monkeypatch.setattr(h.main, "get_component_config", mock.AsyncMock(return_value={}))
    h._runtime.set_llm_router(TokenLLM(["Winds ", "25mph, ", "gusts 15 km/h."]))

    async def scenario(client):
        assert (await post(client, route)).status_code == 200

    rec = one(run(scenario))
    assert rec.response_text == RAW and rec.outcome == "answered"


# ---------------------------------------------------------------------------
# The legacy analytics log and the live event feed carry no conversation text
# ---------------------------------------------------------------------------

def test_the_orchestrator_sends_no_query_text_to_the_legacy_analytics_log_or_the_event_feed(rt):
    logged, emitted = [], []

    async def log_event(session_id, event_type, metadata=None):
        logged.append((event_type, metadata))

    async def session_start(*args, **kwargs):
        emitted.append(kwargs.get("metadata"))

    settings = {"enabled": False, "use_context": False, "history_mode": "full", "max_llm_history_messages": 10}
    fake_config = SimpleNamespace(get_conversation_settings=mock.AsyncMock(return_value=settings), log_analytics_event=log_event)
    rt.monkeypatch.setattr(h.main, "get_config", mock.AsyncMock(return_value=fake_config))
    rt.monkeypatch.setattr(h.main, "EVENTS_AVAILABLE", True)
    rt.monkeypatch.setattr(h.main, "emit_session_start", session_start, raising=False)
    rt.monkeypatch.setattr(h.main, "emit_session_end", mock.AsyncMock(), raising=False)
    rt.monkeypatch.setattr(h.main, "emit_response_ready", mock.AsyncMock(), raising=False)

    async def scenario(client):
        assert (await post(client, "/query", query="a distinctive private question about the garage")).status_code == 200
        await asyncio.sleep(0.1)  # the analytics log is a fire-and-forget task

    run(scenario)
    intent_events = [m for t, m in logged if t == "query_intent"]
    assert intent_events, "the query_intent analytics event is still sent"
    for metadata in intent_events + emitted:
        assert "query" not in metadata and "query_preview" not in metadata
        assert "garage" not in json.dumps(metadata)
    assert intent_events[0]["query_len"] == len("a distinctive private question about the garage")
    assert emitted and emitted[0]["query_len"] == intent_events[0]["query_len"]


def test_the_response_ready_event_carries_a_length_not_a_preview():
    import inspect

    from shared import events

    source = inspect.getsource(events.emit_response_ready)
    assert "response_preview" not in source and "response_len" in source
