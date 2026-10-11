"""Every answer-producing LLM call is capped for speech and unchanged for text.

One scenario per call site (10 in the orchestrator, plus the gateway's Ollama
fallback). A fake router records the `max_tokens` each call receives, with
`interface_type` parametrized over every literal plus an unknown value; the
expectation is derived through `channel_for_interface_type`, not restated.
For speech, an answer the backend cut off at the cap comes back trimmed to a
complete sentence and an answer it finished comes back exactly as generated.
"""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest

from shared.output_channel import OutputChannel, channel_for_interface_type

from . import _public_audience_harness as h

LITERALS = ["voice", "text", "chat"]                    # what OrchestratorState / QueryRequest accept
INTERFACES = LITERALS + ["kiosk"]                       # plus an unknown value, where the site takes a bare string
CAP = 200
LONG_CAP = 600
TOOL_FLOOR = 512
UNFINISHED = "First sentence. Second sentence. Third sen"
TRIMMED = "First sentence. Second sentence."


def is_speech(interface_type):
    return channel_for_interface_type(interface_type) is OutputChannel.SPEECH


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    h.reset_runtime()
    monkeypatch.setattr("orchestrator.helpers.get_voice_response_limits",
                        mock.AsyncMock(return_value={"max_sentences": 3, "max_tokens": CAP, "max_tokens_long": LONG_CAP,
                                                     "ambient_fragment_gate": False}))
    yield
    h.reset_runtime()


class Recorder:
    """A router whose every call is recorded; each method answers `text` and
    reports `stop` as the backend's stop reason."""

    def __init__(self, text=UNFINISHED, stop="stop", scripted_with_tools=()):
        self.text, self.stop = text, stop
        self.calls = []
        self._scripted = list(scripted_with_tools)

    def _result(self, **extra):
        return {"response": self.text, "content": self.text, "stop_reason": self.stop, "eval_count": 5, **extra}

    async def generate(self, **kwargs):
        self.calls.append(("generate", kwargs))
        return self._result()

    async def generate_with_tools(self, **kwargs):
        self.calls.append(("generate_with_tools", kwargs))
        if self._scripted:
            return self._scripted.pop(0)
        return self._result()

    async def generate_stream(self, **kwargs):
        self.calls.append(("generate_stream", kwargs))
        yield {"token": self.text, "done": False}
        yield {"token": "", "done": True, "stop_reason": self.stop, "eval_count": 5}

    def max_tokens(self, index=-1):
        return self.calls[index][1].get("max_tokens", "<absent>")


def expected(interface_type, text_value, *, tool_calling=False, long_form=False):
    if not is_speech(interface_type):
        return text_value
    cap = LONG_CAP if long_form else CAP
    cap = max(cap, TOOL_FLOOR) if tool_calling else cap
    return min(text_value, cap) if text_value else cap


def finished_or_cut(interface_type, stop, text=UNFINISHED):
    """What the answer should be: trimmed only for speech that was cut off."""
    return TRIMMED if (stop == "length" and is_speech(interface_type)) else text


# --- 1. service bypass -------------------------------------------------------------------------


def _bypass(interface_type, recorder, config=None):
    h._runtime.set_llm_router(recorder)
    state = SimpleNamespace(interface_type=interface_type, zone=None, user_context=None, timing_tracker=None)
    config = {"cloud_model": "m", "cloud_provider": "p", **(config or {})}
    return asyncio.run(h.main.handle_query_with_bypass("q", "general_info", config, state=state))


@pytest.mark.parametrize("interface_type", INTERFACES)
@pytest.mark.parametrize("configured", [None, 4096])
def test_bypass_cap(interface_type, configured):
    rec = Recorder()
    _bypass(interface_type, rec, {"max_tokens": configured} if configured else {})
    assert rec.max_tokens() == expected(interface_type, configured or 1024)


@pytest.mark.parametrize("interface_type", INTERFACES)
@pytest.mark.parametrize("stop", ["stop", "length"])
def test_bypass_trim(interface_type, stop):
    assert _bypass(interface_type, Recorder(stop=stop)) == finished_or_cut(interface_type, stop)


# --- 2-4. tool_call_node: selection, fallback synthesis, tool synthesis ----------------------------------


@pytest.fixture
def tool_rig(monkeypatch):
    h.patch_tool_call_dependencies(monkeypatch, admin=h.fake_admin_client())
    return monkeypatch


def _tool_state(interface_type, query="what is going on in the world today", intent=None):
    h.install_mode_client(server_mode="owner")
    authz = asyncio.run(h.mode_permission.resolve_request_authorization(
        "owner", None, caller_trust="household", service_authenticated=False,
    ))
    return h.OrchestratorState(
        query=query, mode=authz.mode, room="kitchen", permissions=authz.permissions,
        intent=intent or h.IntentCategory.GENERAL_INFO, interface_type=interface_type, context={}, session_id=None,
        knowledge_audience=authz.knowledge_audience,
    )


def _component(monkeypatch, max_tokens):
    component = {"model_name": "m", "backend_type": "ollama", "max_tokens": max_tokens}
    monkeypatch.setattr(h.main, "get_component_config", mock.AsyncMock(return_value=component))


@pytest.mark.parametrize("interface_type", LITERALS)
@pytest.mark.parametrize("component_max,text_value", [(None, 200), (900, 900)])
def test_tool_selection_cap(tool_rig, interface_type, component_max, text_value):
    _component(tool_rig, component_max)
    rec = Recorder(scripted_with_tools=[{"content": "Sure.", "stop_reason": "stop"}])
    h._runtime.set_llm_router(rec)
    asyncio.run(h.main.tool_call_node(_tool_state(interface_type)))
    assert rec.calls[0][0] == "generate_with_tools"
    assert rec.max_tokens(0) == expected(interface_type, text_value, tool_calling=True)


@pytest.mark.parametrize("interface_type", LITERALS)
def test_tool_selection_story_and_continue_overrides_are_text_only(tool_rig, interface_type):
    _component(tool_rig, None)
    rec = Recorder(scripted_with_tools=[{"content": "Once.", "stop_reason": "stop"}])
    h._runtime.set_llm_router(rec)
    asyncio.run(h.main.tool_call_node(_tool_state(interface_type, query="tell me a story about a fox")))
    # the long-story allowance exists only off the voice channel; speech keeps the selection default
    story_value = 200 if is_speech(interface_type) else 2000
    assert rec.max_tokens(0) == expected(interface_type, story_value, tool_calling=True)


@pytest.mark.parametrize("interface_type", LITERALS)
@pytest.mark.parametrize("stop", ["stop", "length"])
def test_tool_selection_direct_answer_trim(tool_rig, interface_type, stop):
    _component(tool_rig, None)
    rec = Recorder(scripted_with_tools=[{"content": UNFINISHED, "stop_reason": stop, "eval_count": 5}])
    h._runtime.set_llm_router(rec)
    state = asyncio.run(h.main.tool_call_node(_tool_state(interface_type)))
    assert state.answer == finished_or_cut(interface_type, stop)


def _fallback_state(monkeypatch, interface_type, recorder):
    _component(monkeypatch, None)

    async def fake_search(state, intent, reason):
        state.retrieved_data = {"results": [{"title": "T", "snippet": "S"}]}

    monkeypatch.setattr(h.main, "_fallback_to_web_search", fake_search)
    recorder._scripted = [{"content": "", "stop_reason": "stop"}]
    h._runtime.set_llm_router(recorder)
    return asyncio.run(h.main.tool_call_node(_tool_state(interface_type)))


@pytest.mark.parametrize("interface_type", LITERALS)
@pytest.mark.parametrize("stop", ["stop", "length"])
def test_fallback_synthesis_cap_and_trim(tool_rig, interface_type, stop):
    rec = Recorder(stop=stop)
    state = _fallback_state(tool_rig, interface_type, rec)
    generate_calls = [c for c in rec.calls if c[0] == "generate"]
    assert generate_calls, "the web-search fallback synthesis ran"
    assert generate_calls[0][1].get("max_tokens") == expected(interface_type, None)
    assert state.answer == finished_or_cut(interface_type, stop)


def _synthesis_run(monkeypatch, interface_type, recorder, tool_name="get_weather"):
    _component(monkeypatch, None)
    recorder._scripted = [
        {"content": "", "tool_calls": [h.tool_call(tool_name)], "stop_reason": "stop"},
    ]
    h._runtime.set_llm_router(recorder)
    return asyncio.run(h.main.tool_call_node(_tool_state(interface_type, query="what's the weather in Anytown")))


@pytest.mark.parametrize("interface_type", LITERALS)
def test_tool_synthesis_cap(tool_rig, interface_type):
    rec = Recorder()
    _synthesis_run(tool_rig, interface_type, rec)
    assert len(rec.calls) == 2 and all(c[0] == "generate_with_tools" for c in rec.calls)
    text_value = 3000 if interface_type == "chat" else 800
    assert rec.max_tokens(1) == expected(interface_type, text_value)


@pytest.mark.parametrize("interface_type", LITERALS)
@pytest.mark.parametrize("stop", ["stop", "length"])
def test_tool_synthesis_trim(tool_rig, interface_type, stop):
    state = _synthesis_run(tool_rig, interface_type, Recorder(stop=stop))
    assert state.answer == finished_or_cut(interface_type, stop)


# --- 5. synthesize_node -----------------------------------------------------------------------------------


def _synthesize(monkeypatch, interface_type, recorder):
    h.patch_tool_call_dependencies(monkeypatch, admin=h.fake_admin_client())
    monkeypatch.setattr(h.synthesize_module, "get_component_config",
                        mock.AsyncMock(return_value={"model_name": "m", "backend_type": "ollama"}))
    monkeypatch.setattr(h.synthesize_module, "store_conversation_context", mock.AsyncMock(), raising=False)
    h._runtime.set_llm_router(recorder)
    state = _tool_state(interface_type)
    state.retrieved_data = {"weather": {"current": {"temp": 70}}}
    return asyncio.run(h.synthesize_module.synthesize_node(state))


@pytest.mark.parametrize("interface_type", LITERALS)
def test_synthesize_cap(monkeypatch, interface_type):
    rec = Recorder()
    _synthesize(monkeypatch, interface_type, rec)
    assert rec.max_tokens() == expected(interface_type, None)


@pytest.mark.parametrize("interface_type", LITERALS)
@pytest.mark.parametrize("stop", ["stop", "length"])
def test_synthesize_trim(monkeypatch, interface_type, stop):
    assert _synthesize(monkeypatch, interface_type, Recorder(stop=stop)).answer == finished_or_cut(interface_type, stop)


# --- 6. post-synthesis fallback -----------------------------------------------------------------------------


def _post_fallback(monkeypatch, interface_type, recorder):
    from orchestrator import helpers

    engine = mock.MagicMock()
    engine.search = AsyncMock = mock.AsyncMock(return_value=(None, [SimpleNamespace(snippet="A snippet of web text.")]))
    h._runtime.set_parallel_search_engine(engine)
    h._runtime.set_llm_router(recorder)
    monkeypatch.setattr(helpers, "get_post_synthesis_fallback_config", mock.AsyncMock(return_value={"enabled": True, "config": {}}))
    monkeypatch.setattr(helpers, "get_component_config", mock.AsyncMock(return_value={"model_name": "m"}))
    state = h.OrchestratorState(query="who knows")
    state.answer = "I couldn't find that."
    state.intent = h.IntentCategory.GENERAL_INFO
    state.interface_type = interface_type
    state.permissions = {"mode": "owner"}
    asyncio.run(helpers.maybe_post_synthesis_fallback(state))
    return state


@pytest.mark.parametrize("interface_type", LITERALS)
def test_post_synthesis_fallback_cap(monkeypatch, interface_type):
    rec = Recorder(text=UNFINISHED + " and a bit more text here")
    _post_fallback(monkeypatch, interface_type, rec)
    assert rec.max_tokens() == expected(interface_type, None)


@pytest.mark.parametrize("interface_type", LITERALS)
@pytest.mark.parametrize("stop", ["stop", "length"])
def test_post_synthesis_fallback_trim(monkeypatch, interface_type, stop):
    full = "First sentence here. Second sentence here. Third sen"
    state = _post_fallback(monkeypatch, interface_type, Recorder(text=full, stop=stop))
    assert state.answer == ("First sentence here. Second sentence here." if stop == "length" and is_speech(interface_type) else full)


# --- 7. automation agent direct answer -------------------------------------------------------------------------


def _agent(recorder):
    from orchestrator.automation_agent import AutomationAgent

    return AutomationAgent(ha_client=mock.MagicMock(), llm_router=recorder)


@pytest.mark.parametrize("interface_type", INTERFACES)
def test_automation_agent_cap(interface_type):
    rec = Recorder()
    asyncio.run(_agent(rec)._call_llm_with_tools([{"role": "user", "content": "x"}], "m", interface_type))
    assert rec.max_tokens() == expected(interface_type, 2000, tool_calling=True)


def test_automation_agent_speech_cap_is_the_tool_call_floor_not_the_voice_cap():
    rec = Recorder()
    asyncio.run(_agent(rec)._call_llm_with_tools([{"role": "user", "content": "x"}], "m", "voice"))
    assert rec.max_tokens() == TOOL_FLOOR


@pytest.mark.parametrize("interface_type", INTERFACES)
@pytest.mark.parametrize("stop", ["stop", "length"])
def test_automation_agent_direct_text_trim(interface_type, stop):
    result = asyncio.run(_agent(Recorder(stop=stop))._call_llm_with_tools([{"role": "user", "content": "x"}], "m", interface_type))
    assert result["content"] == finished_or_cut(interface_type, stop)


@pytest.mark.parametrize("interface_type", INTERFACES)
@pytest.mark.parametrize("stop", ["stop", "length"])
def test_automation_agent_execute_threads_the_interface_through(interface_type, stop):
    """The whole agent loop: the context's interface_type reaches the call, and the
    spoken direct reply it returns is the trimmed one."""
    rec = Recorder(stop=stop)
    agent = _agent(rec)
    agent._build_system_prompt = mock.AsyncMock(return_value="system")
    answer = asyncio.run(agent.execute("do the thing", {"interface_type": interface_type, "mode": "owner", "room": "kitchen"}, "m"))
    assert rec.max_tokens() == expected(interface_type, 2000, tool_calling=True)
    assert answer == finished_or_cut(interface_type, stop)


def test_automation_agent_never_trims_a_tool_call():
    call = '{"tool": "ha_service", "arguments": {"domain": "light", "service": "turn_on"}}'
    result = asyncio.run(_agent(Recorder(text=call, stop="length"))._call_llm_with_tools([{"role": "user", "content": "x"}], "m", "voice"))
    assert result["tool_calls"], "a cut-off call is parsed, never trimmed to prose"


def test_route_control_passes_the_interface_type_to_the_agent():
    source = (h.ORCH_DIR / "nodes" / "route_control.py").read_text(encoding="utf-8")
    assert '"interface_type": state.interface_type' in source


# --- 8, 9. the two streaming routes ------------------------------------------------------------------------------------


def _stream_rig(monkeypatch, recorder, component_max=None, intent=None):
    h.reset_runtime()
    h.patch_conversation_config(monkeypatch)
    monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: True)
    h.install_mode_client(server_mode="owner")
    monkeypatch.setattr(h.main, "get_admin_client", lambda: h.fake_admin_client())
    h._runtime.set_cache_client(SimpleNamespace(client=SimpleNamespace(
        delete=mock.AsyncMock(), get=mock.AsyncMock(return_value=None), setex=mock.AsyncMock())))
    h._runtime.set_llm_router(recorder)

    async def run(state):
        state.intent = intent or h.IntentCategory.GENERAL_INFO
        state.answer = ""
        return state

    async def prompt(state):
        return ("prompt", "model", "system")

    monkeypatch.setattr(h.main, "run_orchestrator_for_streaming", run)
    monkeypatch.setattr(h.main, "build_synthesis_prompt_for_streaming", prompt)
    monkeypatch.setattr(h.main, "get_component_config",
                        mock.AsyncMock(return_value={"max_tokens": component_max} if component_max else {}))
    monkeypatch.setattr(h.main, "record_intent_metric", mock.AsyncMock())


async def _post(path, body):
    transport = httpx.ASGITransport(app=h.main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=body, headers=h.service_headers())


def _query_stream(interface_type, query="describe the history of the town"):
    resp = asyncio.run(_post("/query/stream", {"query": query, "interface_type": interface_type, "caller_trust": "household"}))
    events = [json.loads(c[6:]) for c in resp.text.split("\n\n") if c.startswith("data: ")]
    return "".join(e["content"] for e in events if e.get("stage") == "answer_chunk")


def _v1_stream(interface_type, query="describe the history of the town"):
    body = {"model": "m", "stream": True, "messages": [{"role": "user", "content": query}],
            "extra_body": {"interface_type": interface_type}}
    resp = asyncio.run(_post("/v1/chat/completions", body))
    chunks = [json.loads(c[6:]) for c in resp.text.split("\n\n") if c.startswith("data: ") and "[DONE]" not in c]
    return "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)


@pytest.mark.parametrize("interface_type", LITERALS)
@pytest.mark.parametrize("component_max,text_value", [(None, 2048), (700, 700)])
def test_query_stream_cap(monkeypatch, interface_type, component_max, text_value):
    rec = Recorder()
    _stream_rig(monkeypatch, rec, component_max)
    _query_stream(interface_type)
    assert rec.max_tokens() == expected(interface_type, text_value)


@pytest.mark.parametrize("interface_type", LITERALS)
@pytest.mark.parametrize("stop", ["stop", "length"])
def test_query_stream_trim(monkeypatch, interface_type, stop):
    _stream_rig(monkeypatch, Recorder(stop=stop))
    from shared.output_channel import render_answer

    assert _query_stream(interface_type) == render_answer(finished_or_cut(interface_type, stop), interface_type)


@pytest.mark.parametrize("interface_type", INTERFACES)
def test_v1_stream_cap(monkeypatch, interface_type):
    rec = Recorder()
    _stream_rig(monkeypatch, rec)
    _v1_stream(interface_type)
    assert rec.max_tokens() == expected(interface_type, 2048)


@pytest.mark.parametrize("interface_type", INTERFACES)
@pytest.mark.parametrize("stop", ["stop", "length"])
def test_v1_stream_trim(monkeypatch, interface_type, stop):
    _stream_rig(monkeypatch, Recorder(stop=stop))
    from shared.output_channel import render_answer

    spoken = render_answer(finished_or_cut(interface_type, stop), interface_type)
    assert " ".join(_v1_stream(interface_type).split()) == " ".join(spoken.split())


# --- 10. the configured cap reaches the site (not the default) ------------------------------------------------------------


def test_the_admin_set_cap_is_what_a_site_uses(monkeypatch):
    monkeypatch.setattr("orchestrator.helpers.get_voice_response_limits",
                        mock.AsyncMock(return_value={"max_sentences": 2, "max_tokens": 120, "ambient_fragment_gate": False}))
    rec = Recorder()
    _bypass("voice", rec)
    assert rec.max_tokens() == 120
    rec = Recorder()
    _bypass("text", rec)
    assert rec.max_tokens() == 1024


# --- 11. the gateway's Ollama fallback ---------------------------------------------------------------------------------------


def _gateway(monkeypatch, channel, stop, text=UNFINISHED):
    sys.modules.setdefault("prometheus_client", mock.MagicMock())
    import os

    os.environ.setdefault("SERVICE_API_KEY", "test-key-cap-sites")
    os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")
    import gateway.main as gw

    seen = {}

    class _Ollama:
        async def chat(self, **kwargs):
            seen["kwargs"] = kwargs
            yield {"done": True, "message": {"content": text}, "eval_count": 5, "done_reason": stop}

    monkeypatch.setattr(gw, "ollama_client", _Ollama())
    monkeypatch.setattr(gw, "get_voice_response_limits",
                        mock.AsyncMock(return_value={"max_sentences": 3, "max_tokens": CAP, "ambient_fragment_gate": False}))
    monkeypatch.setattr(gw, "_log_metric_to_db", mock.AsyncMock())
    request = gw.ChatCompletionRequest(model="gpt-4", messages=[gw.ChatMessage(role="user", content="hi")])
    response = asyncio.run(gw.route_to_ollama(request, channel=channel))
    return seen["kwargs"], response.choices[0].message.content


@pytest.mark.parametrize("interface_type", INTERFACES)
def test_gateway_fallback_cap(monkeypatch, interface_type):
    channel = channel_for_interface_type(interface_type)
    kwargs, _ = _gateway(monkeypatch, channel, "stop")
    if channel is OutputChannel.SPEECH:
        assert kwargs["num_predict"] == CAP
    else:
        assert "num_predict" not in kwargs          # text: the request is exactly what it was before


@pytest.mark.parametrize("interface_type", INTERFACES)
@pytest.mark.parametrize("stop", ["stop", "length"])
def test_gateway_fallback_trim(monkeypatch, interface_type, stop):
    from shared.output_channel import render_for_channel

    channel = channel_for_interface_type(interface_type)
    _, content = _gateway(monkeypatch, channel, stop)
    assert content == render_for_channel(finished_or_cut(interface_type, stop), channel)


# --- long-form turns (recipes, directions, itineraries, step-by-step) get max_tokens_long on speech -------------------------

RECIPES = h.IntentCategory.RECIPES
LONG_QUERY = "walk me through changing a flat tire"
LONG_FORM = [pytest.param(RECIPES, "give me something to cook", id="recipes-intent"),
             pytest.param(None, LONG_QUERY, id="step-by-step-query")]


@pytest.mark.parametrize("interface_type", INTERFACES)
@pytest.mark.parametrize("intent_name", ["recipes", "directions"])
def test_bypass_long_form_cap(interface_type, intent_name):
    rec = Recorder()
    h._runtime.set_llm_router(rec)
    state = SimpleNamespace(interface_type=interface_type, zone=None, user_context=None, timing_tracker=None)
    asyncio.run(h.main.handle_query_with_bypass(
        "q", intent_name, {"cloud_model": "m", "cloud_provider": "p"}, state=state))
    assert rec.max_tokens() == expected(interface_type, 1024, long_form=True)


@pytest.mark.parametrize("interface_type", LITERALS)
@pytest.mark.parametrize("intent,query", LONG_FORM)
def test_tool_selection_long_form_cap(tool_rig, interface_type, intent, query):
    _component(tool_rig, 900)
    rec = Recorder(scripted_with_tools=[{"content": "Sure.", "stop_reason": "stop"}])
    h._runtime.set_llm_router(rec)
    asyncio.run(h.main.tool_call_node(_tool_state(interface_type, query, intent)))
    assert rec.max_tokens(0) == expected(interface_type, 900, tool_calling=True, long_form=True)
    if is_speech(interface_type):
        assert rec.max_tokens(0) == LONG_CAP > CAP


@pytest.mark.parametrize("interface_type", LITERALS)
@pytest.mark.parametrize("intent,query", LONG_FORM)
@pytest.mark.parametrize("stop", ["stop", "length"])
def test_tool_synthesis_long_form_cap_and_trim(tool_rig, interface_type, intent, query, stop):
    _component(tool_rig, None)
    rec = Recorder(stop=stop)
    tool = "search_recipes" if intent is RECIPES else "get_weather"
    rec._scripted = [{"content": "", "tool_calls": [h.tool_call(tool)], "stop_reason": "stop"}]
    h._runtime.set_llm_router(rec)
    state = asyncio.run(h.main.tool_call_node(_tool_state(interface_type, query, intent)))
    text_value = 3000 if interface_type == "chat" else 800
    assert rec.max_tokens(1) == expected(interface_type, text_value, long_form=True)
    assert state.answer == finished_or_cut(interface_type, stop)


@pytest.mark.parametrize("interface_type", LITERALS)
def test_planning_synthesis_gets_the_long_cap(tool_rig, interface_type):
    """A planning request ("fun things", "show me around") that the long-form phrase list
    doesn't match: text keeps its 1500, speech gets max_tokens_long."""
    from orchestrator.helpers import is_long_form_turn

    assert not is_long_form_turn(None, "show me around with some fun things")
    _component(tool_rig, None)
    rec = Recorder()
    rec._scripted = [{"content": "", "tool_calls": [h.tool_call("get_weather")], "stop_reason": "stop"}]
    h._runtime.set_llm_router(rec)
    asyncio.run(h.main.tool_call_node(_tool_state(interface_type, "show me around with some fun things")))
    assert rec.max_tokens(1) == expected(interface_type, 1500, long_form=True)


@pytest.mark.parametrize("interface_type", LITERALS)
@pytest.mark.parametrize("intent,query", LONG_FORM)
def test_fallback_synthesis_long_form_cap(tool_rig, interface_type, intent, query):
    _component(tool_rig, None)

    async def fake_search(state, intent_name, reason):
        state.retrieved_data = {"results": [{"title": "T", "snippet": "S"}]}

    tool_rig.setattr(h.main, "_fallback_to_web_search", fake_search)
    rec = Recorder()
    rec._scripted = [{"content": "", "stop_reason": "stop"}]
    h._runtime.set_llm_router(rec)
    asyncio.run(h.main.tool_call_node(_tool_state(interface_type, query, intent)))
    generate_calls = [c for c in rec.calls if c[0] == "generate"]
    assert generate_calls[0][1].get("max_tokens") == expected(interface_type, None, long_form=True)


@pytest.mark.parametrize("interface_type", LITERALS)
@pytest.mark.parametrize("intent,query", LONG_FORM)
@pytest.mark.parametrize("stop", ["stop", "length"])
def test_synthesize_long_form_cap_and_trim(monkeypatch, interface_type, intent, query, stop):
    rec = Recorder(stop=stop)
    h.patch_tool_call_dependencies(monkeypatch, admin=h.fake_admin_client())
    monkeypatch.setattr(h.synthesize_module, "get_component_config",
                        mock.AsyncMock(return_value={"model_name": "m", "backend_type": "ollama"}))
    monkeypatch.setattr(h.synthesize_module, "store_conversation_context", mock.AsyncMock(), raising=False)
    h._runtime.set_llm_router(rec)
    state = _tool_state(interface_type, query, intent)
    state.retrieved_data = {"x": {"y": 1}}
    result = asyncio.run(h.synthesize_module.synthesize_node(state))
    assert rec.max_tokens() == expected(interface_type, None, long_form=True)
    assert result.answer == finished_or_cut(interface_type, stop)


@pytest.mark.parametrize("interface_type", LITERALS)
def test_post_synthesis_fallback_long_form_cap(monkeypatch, interface_type):
    from orchestrator import helpers

    engine = mock.MagicMock()
    engine.search = mock.AsyncMock(return_value=(None, [SimpleNamespace(snippet="A snippet of web text.")]))
    h._runtime.set_parallel_search_engine(engine)
    rec = Recorder(text=UNFINISHED + " and a bit more text here")
    h._runtime.set_llm_router(rec)
    monkeypatch.setattr(helpers, "get_post_synthesis_fallback_config", mock.AsyncMock(return_value={"enabled": True, "config": {}}))
    monkeypatch.setattr(helpers, "get_component_config", mock.AsyncMock(return_value={"model_name": "m"}))
    state = h.OrchestratorState(query="a recipe for pancakes")
    state.answer = "I couldn't find that."
    state.intent = RECIPES
    state.interface_type = interface_type
    state.permissions = {"mode": "owner"}
    asyncio.run(helpers.maybe_post_synthesis_fallback(state))
    assert rec.max_tokens() == expected(interface_type, None, long_form=True)


@pytest.mark.parametrize("interface_type", LITERALS)
@pytest.mark.parametrize("intent,query", LONG_FORM)
def test_streams_long_form_cap(monkeypatch, interface_type, intent, query):
    rec = Recorder()
    _stream_rig(monkeypatch, rec, intent=intent)
    _query_stream(interface_type, query)
    assert rec.max_tokens() == expected(interface_type, 2048, long_form=True)
    rec = Recorder()
    _stream_rig(monkeypatch, rec, intent=intent)
    _v1_stream(interface_type, query)
    assert rec.max_tokens() == expected(interface_type, 2048, long_form=True)


def test_a_long_form_answer_that_hits_the_long_cap_is_still_trimmed(monkeypatch):
    _stream_rig(monkeypatch, Recorder(stop="length"), intent=RECIPES)
    from shared.output_channel import render_answer

    assert _query_stream("voice", "give me something to cook") == render_answer(TRIMMED, "voice")


def test_the_ordinary_turn_keeps_the_short_cap(monkeypatch):
    rec = Recorder()
    _stream_rig(monkeypatch, rec)
    _query_stream("voice")
    assert rec.max_tokens() == CAP


# --- streams: TEXT tokens are emitted exactly as generated, whatever the final chunk says -----------------------------------


class _TokenRecorder(Recorder):
    async def generate_stream(self, **kwargs):
        self.calls.append(("generate_stream", kwargs))
        for token in ("First sentence. ", "Second sentence. ", "Third sen"):
            yield {"token": token, "done": False}
        yield {"token": "", "done": True, "stop_reason": "length", "eval_count": 5}


@pytest.mark.parametrize("interface_type", ["text", "chat"])
def test_text_streams_keep_every_token_untouched_after_a_length_stop(monkeypatch, interface_type):
    _stream_rig(monkeypatch, _TokenRecorder())
    resp = asyncio.run(_post("/query/stream", {"query": "tell me about it", "interface_type": interface_type, "caller_trust": "household"}))
    events = [json.loads(c[6:]) for c in resp.text.split("\n\n") if c.startswith("data: ")]
    tokens = [e["content"] for e in events if e.get("stage") == "answer_chunk"]
    assert tokens == ["First sentence. ", "Second sentence. ", "Third sen"]
    body = {"model": "m", "stream": True, "messages": [{"role": "user", "content": "tell me about it"}],
            "extra_body": {"interface_type": interface_type}}
    resp = asyncio.run(_post("/v1/chat/completions", body))
    chunks = [json.loads(c[6:]) for c in resp.text.split("\n\n") if c.startswith("data: ") and "[DONE]" not in c]
    assert [c["choices"][0]["delta"].get("content") for c in chunks if c["choices"][0]["delta"].get("content")] == [
        "First sentence. ", "Second sentence. ", "Third sen"]


def test_a_voice_stream_after_a_length_stop_is_trimmed_as_one_piece(monkeypatch):
    _stream_rig(monkeypatch, _TokenRecorder())
    resp = asyncio.run(_post("/query/stream", {"query": "tell me about it", "interface_type": "voice", "caller_trust": "household"}))
    events = [json.loads(c[6:]) for c in resp.text.split("\n\n") if c.startswith("data: ")]
    assert [e["content"] for e in events if e.get("stage") == "answer_chunk"] == [TRIMMED]


# --- /query/stream/v2 renders the graph's finished answer (the site already trimmed it) -----------------------------------------


def _v2(monkeypatch, interface_type, answer):
    _stream_rig(monkeypatch, Recorder())

    class _Graph:
        async def ainvoke(self, state):
            return {"intent": h.IntentCategory.GENERAL_INFO, "answer": answer, "confidence": 1.0, "citations": [],
                    "request_id": "r", "node_timings": {}, "validation_passed": True}

    monkeypatch.setattr(h.main, "orchestrator_graph", _Graph())
    resp = asyncio.run(_post("/query/stream/v2", {"query": "tell me about it", "interface_type": interface_type, "caller_trust": "household"}))
    events = [json.loads(c[6:]) for c in resp.text.split("\n\n") if c.startswith("data: ")]
    return [e["sentence"] for e in events if e.get("stage") == "streaming"], next(e for e in events if e.get("stage") == "complete")


@pytest.mark.parametrize("interface_type", LITERALS)
def test_v2_emits_the_graphs_answer_without_trimming_it_again(monkeypatch, interface_type):
    from shared.output_channel import render_answer

    sentences, complete = _v2(monkeypatch, interface_type, UNFINISHED)
    assert complete["full_response"] == render_answer(UNFINISHED, interface_type)
    assert " ".join(sentences) == complete["full_response"]
    assert "Third sen" in complete["full_response"], "the v2 route adds no cut of its own"


@pytest.mark.parametrize("interface_type", LITERALS)
def test_v2_with_a_trimmed_graph_answer_emits_exactly_that(monkeypatch, interface_type):
    from shared.output_channel import render_answer

    sentences, complete = _v2(monkeypatch, interface_type, TRIMMED)
    assert complete["full_response"] == render_answer(TRIMMED, interface_type)
    assert sentences[-1].endswith("sentence.")


# --- the agent's chat_with_tools branch is a test seam: the real router never takes it ---------------------------------------------


def test_the_real_router_has_no_chat_with_tools_so_the_uncapped_branch_is_unreachable():
    """automation_agent._call_llm_with_tools prefers `chat_with_tools` when the router has it.
    The real LLMRouter doesn't, so production always takes the capped generate() branch. If
    the router ever grows this method, that branch must adopt the voice cap first."""
    from shared.llm_router import LLMRouter

    assert not hasattr(LLMRouter, "chat_with_tools")
