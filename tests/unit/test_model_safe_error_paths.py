"""Backend error text never reaches the model, the speaker or an API client.

Behaviour through the real code: `tool_call_node` with the real
`execute_tools_parallel` and a failing RAG client (what the model is handed,
what the user hears, what the metrics keep), the real `RAGClient` turning a 4xx
body into a vetted `UserSafeText`, the real `AutomationAgent` and `MusicHandler`
with a raising Home Assistant client, and the streaming and HTTP error paths.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest

from orchestrator.model_safe_errors import PHRASES
from orchestrator.rag_client import RAGClient

from . import _model_safe_harness as m
from . import _public_audience_harness as h

OPERATOR_TOKENS = ["10.0.0.5", "8030", "TESLAMATE_DB_HOST", "DEFAULT_AMTRAK_STATION", "ConnectError", "Traceback", "://"]


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


def assert_clean(text):
    for token in OPERATOR_TOKENS:
        assert token not in text, f"{token!r} reached {text[:200]!r}"


# --- tool_call_node ----------------------------------------------------------------------------------------


def test_tool_failures_reach_the_model_as_phrases_while_metrics_keep_the_raw_text(monkeypatch):
    rag = m.FakeRag({
        "weather": httpx.ConnectError("All connection attempts failed http://10.0.0.5:8030/x"),
        "tesla": m.failed("Service returned status 503: TESLAMATE_DB_HOST is not configured", 503),
    })
    llm = m.ToolLLM([m.call("get_weather", "c1", location="Anytown"), m.call("get_tesla_metrics", "c2")])
    metric = m.install(monkeypatch, rag, llm)
    state = m.run_tool_call()

    for message_list in llm.message_lists:
        assert_clean(json.dumps(message_list))
    contents = llm.tool_messages()
    assert len(contents) == 2
    assert json.loads(contents[0])["error"] == PHRASES["unavailable"]
    assert json.loads(contents[1])["error"] == PHRASES["unavailable"] or json.loads(contents[1])["error"] == PHRASES["not_configured"]
    assert_clean(state.answer or "")

    raw = " ".join(str(c.kwargs.get("error_message")) for c in metric.await_args_list)
    assert "10.0.0.5" in raw and "TESLAMATE_DB_HOST" in raw, "the metric rows keep the raw text for operators"


def test_a_compliant_4xx_detail_arrives_byte_identical_from_a_real_rag_client(monkeypatch):
    class _Response:
        status_code = 404

        def json(self):
            return {"detail": "No trains found for that date"}

    class _Client:
        async def request(self, **kwargs):
            return _Response()

    class _Pool:
        async def get_client(self, name):
            return _Client()

    rag = RAGClient(service_urls={"amtrak": "http://amtrak-svc"})
    rag._http_pool = _Pool()
    llm = m.ToolLLM([m.call("get_train_schedule", "c1", origin="NYP", destination="WAS")])
    m.install(monkeypatch, rag, llm)
    m.run_tool_call()
    assert llm.tool_messages() == [json.dumps({"error": "No trains found for that date"})]


def test_the_amtrak_400_detail_becomes_the_bad_request_phrase_end_to_end(monkeypatch):
    class _Response:
        status_code = 400

        def json(self):
            return {"detail": "No origin specified and DEFAULT_AMTRAK_STATION is not configured"}

    class _Client:
        async def request(self, **kwargs):
            return _Response()

    class _Pool:
        async def get_client(self, name):
            return _Client()

    rag = RAGClient(service_urls={"amtrak": "http://amtrak-svc"})
    rag._http_pool = _Pool()
    llm = m.ToolLLM([m.call("get_train_schedule", "c1")])
    metric = m.install(monkeypatch, rag, llm)
    m.run_tool_call()
    assert llm.tool_messages() == [json.dumps({"error": PHRASES["bad_request"]})]
    assert "DEFAULT_AMTRAK_STATION" in str(metric.await_args_list[-1].kwargs.get("error_message")), "operators still see it in the metric row"


def test_the_rag_response_error_field_keeps_the_raw_text(monkeypatch):
    """test_rag_client_dc9.py pins this contract; asserted here from the other side."""
    class _Response:
        status_code = 400

        def json(self):
            return {"detail": "DEFAULT_AMTRAK_STATION is not configured"}

    class _Client:
        async def request(self, **kwargs):
            return _Response()

    class _Pool:
        async def get_client(self, name):
            return _Client()

    rag = RAGClient(service_urls={"amtrak": "http://amtrak-svc"})
    rag._http_pool = _Pool()
    response = asyncio.run(rag.request("amtrak", "GET", "/x", skip_circuit_breaker=True, skip_rate_limit=True))
    assert response.error == "Service returned status 400: DEFAULT_AMTRAK_STATION is not configured"
    assert response.user_detail is None


# --- the automation agent ---------------------------------------------------------------------------------------


def _agent(ha):
    from orchestrator.automation_agent import AutomationAgent

    return AutomationAgent(ha_client=ha, llm_router=mock.MagicMock())


def test_a_failing_ha_service_call_does_not_leak_into_the_tool_result():
    ha = mock.MagicMock()
    ha.call_service = mock.AsyncMock(side_effect=RuntimeError("401 at http://10.0.0.5:8123/api TESLAMATE_DB_HOST"))
    agent = _agent(ha)
    with h.mode_permission.ha_permission_scope({"mode": "owner"}, mode="owner"):
        result = asyncio.run(agent._execute_tool(
            "ha_service", {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"},
            {"mode": "owner", "room": "kitchen"},
        ))
    assert "Failed to call light.turn_on" in result or "Error executing" in result
    assert_clean(result)


def test_a_crashing_agent_loop_speaks_a_fixed_sentence():
    llm = mock.MagicMock()
    llm.generate = mock.AsyncMock(side_effect=RuntimeError("boom http://10.0.0.5:8030 TESLAMATE_DB_HOST"))
    from orchestrator.automation_agent import AutomationAgent

    agent = AutomationAgent(ha_client=mock.MagicMock(), llm_router=llm)
    agent._build_system_prompt = mock.AsyncMock(return_value="system")
    answer = asyncio.run(agent.execute("do the thing", {"mode": "owner", "room": "kitchen"}, "m"))
    assert answer == "Sorry, something went wrong with that request."


def test_agent_tool_messages_are_scrubbed():
    from orchestrator.automation_agent import AutomationAgent

    seen = []

    class _LLM:
        def __init__(self):
            self.calls = 0

        async def generate(self, **kwargs):
            self.calls += 1
            seen.append(kwargs["prompt"])
            if self.calls == 1:
                return {"response": '{"tool": "get_entity_state", "arguments": {"entity_id": "light.x"}}'}
            return {"response": 'done("ok")'}

    agent = AutomationAgent(ha_client=mock.MagicMock(), llm_router=_LLM())
    agent._build_system_prompt = mock.AsyncMock(return_value="system")

    async def execute_tool(name, args, context):
        return {"error": "failed at http://10.0.0.5/x TESLAMATE_DB_HOST"} if name == "get_entity_state" else "ok"

    agent._execute_tool = execute_tool
    asyncio.run(agent.execute("what is on", {"mode": "owner", "room": "kitchen"}, "m"))
    assert len(seen) == 2
    assert_clean(seen[1])


# --- the music handler speaks a fixed sentence -------------------------------------------------------------------


def test_a_music_pause_failure_speaks_the_fixed_sentence():
    from orchestrator.music_handler import MusicHandler

    ha = mock.MagicMock()
    ha.call_service = mock.AsyncMock(side_effect=RuntimeError("boom http://10.0.0.5:8123 TESLAMATE_DB_HOST"))
    handler = MusicHandler(ha_client=ha, admin_client=mock.MagicMock())
    handler._get_entity_for_room = mock.AsyncMock(return_value="media_player.kitchen")
    handler.playback_manager.is_playing = lambda room: True
    answer = asyncio.run(handler.handle_room_specific_pause("kitchen"))
    assert answer == "Sorry, I couldn't pause in kitchen."
    assert_clean(answer)


# --- streaming and HTTP error paths --------------------------------------------------------------------------------


def _boom_rig(monkeypatch):
    from . import _fast_path_harness as fp

    fp.install(monkeypatch, slow="record")

    async def run(state):
        raise RuntimeError("explode at http://10.0.0.5:8030 TESLAMATE_DB_HOST")

    class _Graph:
        async def ainvoke(self, state):
            raise RuntimeError("explode at http://10.0.0.5:8030 TESLAMATE_DB_HOST")

    monkeypatch.setattr(h.main, "run_orchestrator_for_streaming", run)
    monkeypatch.setattr(h.main, "orchestrator_graph", _Graph())
    return fp


@pytest.mark.parametrize("route", ["query_stream", "query_stream_v2"])
def test_a_stream_error_event_is_fixed_text(monkeypatch, route):
    fp = _boom_rig(monkeypatch)
    resp = asyncio.run(fp.send(route, "tell me about the town", interface_type="text"))
    events = [json.loads(e) for e in fp._events(resp.text)]
    errors = [e for e in events if e.get("stage") == "error"]
    assert errors and all(e["message"] == h.main.STREAM_ERROR_MESSAGE for e in errors)
    assert_clean(resp.text)


def test_the_query_route_returns_an_opaque_500(monkeypatch):
    fp = _boom_rig(monkeypatch)
    resp = asyncio.run(fp.send("query", "tell me about the town", interface_type="text", headers={"X-Request-ID": "req-abc-123"}))
    assert resp.status_code == 500 and resp.json() == {"detail": "internal_error request_id=req-abc-123"}
    assert resp.headers["X-Request-ID"] == "req-abc-123"
    assert_clean(resp.text)


def test_the_v1_route_returns_an_opaque_500(monkeypatch):
    fp = _boom_rig(monkeypatch)
    resp = asyncio.run(fp.send("v1_nonstream", "tell me about the town", interface_type="text", headers={"X-Request-ID": "req-def-456"}))
    assert resp.status_code == 500 and resp.json() == {"detail": "internal_error request_id=req-def-456"}
    assert_clean(resp.text)


def test_a_500_without_a_request_id_header_still_carries_the_generated_one(monkeypatch):
    fp = _boom_rig(monkeypatch)
    resp = asyncio.run(fp.send("query", "tell me about the town", interface_type="text"))
    detail = resp.json()["detail"]
    assert detail.startswith("internal_error request_id=") and detail.split("=", 1)[1] not in ("", "unknown")
    assert detail.split("=", 1)[1] == resp.headers["X-Request-ID"]


def test_a_websearch_error_is_a_phrase_in_the_data_source():
    import ast
    from pathlib import Path

    source = (h.ORCH_DIR / "nodes" / "retrieve.py").read_text(encoding="utf-8")
    assert "model_safe_error(error_msg" in source and "websearch error: {error_msg}" not in source
    ast.parse(source)
    _ = (Path, SimpleNamespace)


# --- the web-search fallback and tool creation ---------------------------------------------------------------------


def test_a_failed_tools_raw_error_never_reaches_the_fallback_note(monkeypatch):
    """A RAG service can answer 200 with an `error` field holding operator text. When the web fallback
    replaces that result, its `fallback_reason` is built from the error, so the error is made safe first."""
    raw = "db at 10.0.0.5:8030 failed: TESLAMATE_DB_HOST ConnectError"
    rag = m.FakeRag({"dining": m.ok({"error": raw})})
    llm = m.ToolLLM([m.call("search_restaurants", "c1", query="pizza")])
    m.install(monkeypatch, rag, llm)
    result = SimpleNamespace(to_dict=lambda: {"title": "Pizza Place", "snippet": "good"})
    engine = mock.MagicMock()
    engine.search = mock.AsyncMock(return_value=("restaurants", [result]))
    h._runtime.set_parallel_search_engine(engine)
    m.run_tool_call("find pizza near me")
    engine.search.assert_awaited()
    sent = json.dumps(llm.message_lists[-1])
    assert "web_search_fallback" in sent, "the fallback ran, so the note was built"
    assert_clean(sent)


def test_a_failed_tool_with_a_failed_fallback_keeps_only_phrases(monkeypatch):
    rag = m.FakeRag({"dining": m.ok({"error": "db at 10.0.0.5 TESLAMATE_DB_HOST"})})
    llm = m.ToolLLM([m.call("search_restaurants", "c1", query="pizza")])
    m.install(monkeypatch, rag, llm)
    engine = mock.MagicMock()
    engine.search = mock.AsyncMock(side_effect=RuntimeError("search exploded at http://10.0.0.5:9000"))
    h._runtime.set_parallel_search_engine(engine)
    m.run_tool_call("find pizza near me")
    sent = json.dumps(llm.message_lists[-1])
    assert "fallback_attempted" in sent
    assert_clean(sent)


def test_a_failed_tool_creation_speaks_a_fixed_sentence(monkeypatch):
    manager = mock.MagicMock()
    manager.check_enabled = mock.AsyncMock(return_value=True)
    monkeypatch.setattr(h.main.SelfBuildingToolsFactory, "get", lambda: manager)
    monkeypatch.setattr(h.main, "generate_tool_from_request", mock.AsyncMock(
        return_value={"success": False, "error": "Invalid JSON from LLM at http://10.0.0.5:11434 TESLAMATE_DB_HOST"}))
    h._runtime.set_llm_router(mock.MagicMock())
    result = asyncio.run(h.main.handle_query_with_bypass.__globals__["handle_tool_creation_request"]("create a tool that x", "s", "owner"))
    assert result["success"] is False
    assert result["answer"] == "I couldn't generate a tool definition. Please try describing what you need more specifically."
    assert_clean(result["answer"])


def test_the_unauthenticated_status_routes_do_not_return_exception_text(monkeypatch):
    import inspect

    source = inspect.getsource(h.main)
    assert 'health["resilience"] = {"error": "internal_error"}' in source
    assert '{"status": "error", "error": "internal_error"}' in source
    assert 'health["resilience"] = {"error": str(e)}' not in source


def test_tv_handler_results_carry_a_phrase_not_the_exception():
    import ast

    source = (h.ORCH_DIR / "tv_handler.py").read_text(encoding="utf-8")
    assert '"error": str(e)' not in source and source.count('"error": model_safe_error(e)') == 5
    ast.parse(source)
