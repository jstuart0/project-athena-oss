"""The deterministic fast path, black-box through every query route.

The graph, `classify_node`, the streaming runner and every LLM-router method
are fakes that fail the test when reached, so a passing answer proves zero
model calls. Scenarios run inside one event loop (`asyncio.run`) so the
background persistence task and the session read share it.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from . import _public_audience_harness as h
from . import _fast_path_harness as fp

GREETING = "Hello. How can I help?"
THANKS = "You're welcome."


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("route", fp.ROUTES)
def test_greeting_is_answered_with_no_model_call(monkeypatch, route):
    rig = fp.install(monkeypatch)

    async def scenario():
        resp = await fp.send(route, "Hello!")
        return fp.answer_of(route, resp), await fp.stored_messages(route)

    answer, messages = _run(scenario())
    assert answer == GREETING
    assert rig.reached == [] and rig.llm.calls == []
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "Hello!"
    assert messages[1]["content"] == GREETING
    assert messages[1]["metadata"] == {"fast_path": "greeting"}
    rig.metric.assert_awaited_once()
    assert rig.metric.await_args.kwargs["intent"] == "general_info"
    assert rig.metric.await_args.kwargs["confidence"] == 1.0


@pytest.mark.parametrize("route", fp.ROUTES)
def test_each_route_labels_its_metrics(monkeypatch, route):
    rig = fp.install(monkeypatch)
    answered = mock.MagicMock()
    seconds = mock.MagicMock()
    monkeypatch.setattr(h.main, "fast_path_answered_total", answered)
    monkeypatch.setattr(h.main, "fast_path_seconds", seconds)
    expected_route = {"v1_nonstream": "query"}.get(route, route)

    _run(fp.send(route, "thanks"))
    answered.labels.assert_called_once_with(route=expected_route, kind="thanks")
    seconds.labels.assert_called_once_with(route=expected_route)
    assert rig.reached == []


def test_query_route_returns_the_normal_shape(monkeypatch):
    fp.install(monkeypatch)
    body = _run(fp.send("query", "what time is it", interface_type="text")).json()
    assert body["intent"] == "general_info"
    assert body["confidence"] == 1.0
    assert body["metadata"] == {"fast_path": "time"}
    assert body["answer"].startswith("It's ") and body["session_id"] == fp.session_id_for("query")
    assert body["citations"] == [] and body["request_id"]


def test_stream_routes_keep_their_stage_names(monkeypatch):
    fp.install(monkeypatch)
    stages = {}
    for route in ("query_stream", "query_stream_v2"):
        resp = _run(fp.send(route, "thanks"))
        stages[route] = [json.loads(e).get("stage") for e in fp._events(resp.text)]
    assert stages["query_stream"][-2:] == ["answer_chunk", "complete"]
    assert stages["query_stream_v2"][-3:] == ["classified", "streaming", "complete"]
    v2 = [json.loads(e) for e in fp._events(_run(fp.send("query_stream_v2", "thanks")).text)]
    assert [e for e in v2 if e["stage"] == "streaming"][0]["is_final"] is True
    assert [e for e in v2 if e["stage"] == "complete"][0]["intent"] == "general_info"


def test_v1_stream_emits_content_stop_and_done(monkeypatch):
    fp.install(monkeypatch)
    resp = _run(fp.send("v1_stream", "thanks"))
    events = fp._events(resp.text)
    chunks = [json.loads(e) for e in events[:-1]]
    assert events[-1] == "[DONE]"
    assert chunks[0]["choices"][0]["delta"]["content"] == THANKS
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert len({c["id"] for c in chunks}) == 1


# --- speech rendering -------------------------------------------------------------


@pytest.mark.parametrize("route", fp.ROUTES)
@pytest.mark.parametrize("interface_type", ["voice", "text", "chat"])
def test_reply_is_rendered_by_interface_type_and_stored_raw(monkeypatch, route, interface_type):
    from shared.output_channel import render_answer

    fp.install(monkeypatch)
    raw = "It's 9:05 AM."
    monkeypatch.setattr(h.main, "fast_path_reply", lambda q: SimpleNamespace(kind="time", text=raw))

    async def scenario():
        resp = await fp.send(route, "what time is it", interface_type=interface_type)
        return fp.answer_of(route, resp), await fp.stored_messages(route)

    answer, messages = _run(scenario())
    assert answer == render_answer(raw, interface_type)
    assert messages[1]["content"] == raw


def test_voice_rendering_actually_changes_the_text(monkeypatch):
    """Guards the test above against passing vacuously."""
    from shared.output_channel import render_answer

    assert render_answer("Winds 25mph.", "voice") != "Winds 25mph."
    assert render_answer("Winds 25mph.", "text") == "Winds 25mph."


# --- owner PIN still wins -----------------------------------------------------------


@pytest.mark.parametrize("route", fp.ROUTES)
def test_pin_utterance_wins_over_the_fast_path(monkeypatch, route):
    rig = fp.install(monkeypatch)
    outcome = SimpleNamespace(message="Owner mode on.", success=True, override_data=None, refused_reason=None)
    monkeypatch.setattr(h.main, "handle_owner_mode_utterance", mock.AsyncMock(return_value=outcome))
    spy = mock.AsyncMock(wraps=h.main._fast_path_turn)
    monkeypatch.setattr(h.main, "_fast_path_turn", spy)

    answer = fp.answer_of(route, _run(fp.send(route, "hello")))
    assert "Owner mode on" in answer
    spy.assert_not_awaited()
    assert rig.reached == []


# --- authorization ------------------------------------------------------------------


GUEST_PROFILE_WITHOUT_GENERAL_INFO = {
    "mode": "guest", "allowed_intents": ["weather"], "restricted_entities": [], "allowed_domains": ["light"],
}


@pytest.mark.parametrize("route", fp.ROUTES)
def test_a_refused_general_info_intent_is_not_shortcut(monkeypatch, route):
    rig = fp.install(monkeypatch, server_mode="guest", guest_profile=GUEST_PROFILE_WITHOUT_GENERAL_INFO, slow="record")
    from orchestrator.mode_permission import intent_gate_refusal

    resp = _run(fp.send(route, "hello"))
    assert resp.status_code == 200
    assert rig.reached, "the full pipeline ran, so the gate's own refusal path decides"
    assert GREETING not in fp.answer_of(route, resp)
    permissions = {"mode": "guest", "allowed_intents": ["weather"]}
    assert intent_gate_refusal(h.IntentCategory.GENERAL_INFO, permissions)


@pytest.mark.parametrize("route", fp.ROUTES)
def test_a_gate_refusal_defers_even_when_the_table_matches(monkeypatch, route):
    rig = fp.install(monkeypatch, slow="record")
    monkeypatch.setattr(h.main, "intent_gate_refusal", lambda intent, permissions: "REFUSED")
    answer = fp.answer_of(route, _run(fp.send(route, "hello")))
    assert rig.reached
    assert answer != GREETING


def test_missing_permissions_never_take_the_fast_path(monkeypatch):
    fp.install(monkeypatch)
    reads = mock.AsyncMock(return_value=None)
    monkeypatch.setattr(h.main, "fast_path_open_question", reads)

    async def call(permissions):
        return await h.main._fast_path_turn(
            "hello", permissions=permissions, session=SimpleNamespace(messages=[]), session_id="s",
            route="query", room="kitchen", mode="owner", request_id="r", handler_start=0.0,
        )

    assert _run(call(None)) is None
    reads.assert_not_awaited()
    assert _run(call({"mode": "owner"})).kind == "greeting"
    reads.assert_awaited_once()


def test_non_candidates_never_read_the_context(monkeypatch):
    fp.install(monkeypatch, slow="record")
    reads = mock.AsyncMock(return_value=None)
    monkeypatch.setattr(h.main, "fast_path_open_question", reads)
    answer = fp.answer_of("query", _run(fp.send("query", "who won the game last night")))
    assert answer == fp.SLOW_ANSWER
    reads.assert_not_awaited()


# --- the scene and confirmation vocabulary is never shortcut ---------------------------


@pytest.mark.parametrize("phrase", ["good morning", "goodbye", "good night", "okay", "yes", "no thanks", "do it"])
def test_scene_and_confirmation_phrases_reach_the_pipeline(monkeypatch, phrase):
    rig = fp.install(monkeypatch, slow="record")
    answer = fp.answer_of("query", _run(fp.send("query", phrase)))
    assert answer == fp.SLOW_ANSWER
    assert rig.reached == ["graph"]


# --- persistence is awaited before the response completes ---------------------------


def test_a_fast_follow_up_sees_both_fast_path_messages(monkeypatch):
    """No drain between the turns: the first /query has already written both messages."""
    rig = fp.install(monkeypatch)

    async def scenario():
        await fp.send("query", "hello")
        session = await h._runtime.get_session_manager().get_session(fp.session_id_for("query"))
        after_first = [(m["role"], m["content"]) for m in session.messages]
        await fp.send("query", "thanks")
        session = await h._runtime.get_session_manager().get_session(fp.session_id_for("query"))
        return after_first, [(m["role"], m["content"]) for m in session.messages]

    after_first, after_second = _run(scenario())
    assert after_first == [("user", "hello"), ("assistant", "Hello. How can I help?")]
    assert after_second == after_first + [("user", "thanks"), ("assistant", "You're welcome.")]
    assert rig.reached == []


@pytest.mark.parametrize("route", ["query_stream", "query_stream_v2", "v1_stream"])
def test_a_client_that_disconnects_at_the_final_event_still_has_the_turn_persisted(monkeypatch, route):
    fp.install(monkeypatch)

    async def scenario():
        chunks = await fp.send_then_disconnect_at_final_event(route, "hello")
        session = await h._runtime.get_session_manager().get_session(fp.session_id_for(route))
        return chunks, [(m["role"], m["content"]) for m in session.messages] if session else []

    chunks, messages = _run(scenario())
    assert any('"stage": "complete"' in c or "[DONE]" in c for c in chunks), "the final event was sent"
    assert messages == [("user", "hello"), ("assistant", "Hello. How can I help?")]
