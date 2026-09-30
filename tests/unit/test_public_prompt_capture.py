"""Black-box prompt and tool-argument capture for the public audience (V1.3).

Each query entry point runs with caller_trust="web_public" while every
source of private data is primed: a request context carrying a guest name,
a device lookup that returns a guest, base knowledge with a Wi-Fi password
and a street address, and a home-address lookup returning that street.
Everything handed to the LLM and to the tool executor is captured; none of
it may carry the private data. A household control run proves the capture
would have seen it.

The graph is replaced by a composition of the real prompt-building nodes
(tool_call_node, then synthesize_node over retrieved data), fed the real
initial state each endpoint builds; /query/stream runs the real
run_orchestrator_for_streaming with classification stubbed.
"""
from __future__ import annotations

from unittest import mock

import pytest
from fastapi.testclient import TestClient

from . import _public_audience_harness as h

PRIVATE_MARKERS = ("Alice", "You are speaking with", h.SECRET_WIFI, h.HOME_STREET)


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


class _NodesGraph:
    async def ainvoke(self, state):
        state.intent = h.IntentCategory.WEATHER
        state = await h.main.tool_call_node(state)
        synth = state.model_copy()
        synth.answer = None
        synth.skip_synthesis = False
        synth.retrieved_data = {"weather": {"current": {"temp": 70}}}
        await h.synthesize_module.synthesize_node(synth)
        return dict(state.__dict__)


@pytest.fixture
def rig(monkeypatch):
    h.patch_conversation_config(monkeypatch)
    monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: True)
    h.install_mode_client(server_mode="owner")
    admin = h.fake_admin_client(guest_info={"guest_id": 7, "guest_name": h.GUEST_NAME})
    _, execute, knowledge, home = h.patch_tool_call_dependencies(monkeypatch, admin=admin)
    monkeypatch.setattr(h.synthesize_module, "get_component_config",
                        mock.AsyncMock(return_value={"model_name": "m", "backend_type": "ollama"}))
    monkeypatch.setattr(h.synthesize_module, "store_conversation_context", mock.AsyncMock(), raising=False)
    llm = h.CapturingLLM(tool_calls=[h.tool_call("get_weather", arguments={"location": "Anytown"})])
    h._runtime.set_llm_router(llm)
    monkeypatch.setattr(h.main, "orchestrator_graph", _NodesGraph())

    async def _classify(state):
        state.intent = h.IntentCategory.GENERAL_INFO
        return state

    monkeypatch.setattr(h.main, "classify_node", _classify)
    return {
        "client": TestClient(h.main.app), "llm": llm, "execute": execute,
        "knowledge": knowledge, "home": home, "admin": admin, "monkeypatch": monkeypatch,
    }


def _post(rig, path, caller_trust):
    body = {
        "query": "what's the weather",
        "caller_trust": caller_trust,
        "device_id": "dev1",
        "interface_type": "chat",
        "context": {"guest_id": 7, "guest_name": h.GUEST_NAME},
    }
    with rig["client"].stream("POST", path, json=body, headers=h.service_headers()) as resp:
        assert resp.status_code == 200
        return "".join(resp.iter_text())


def _executor_text(execute) -> str:
    return "\n".join(repr(call) for call in execute.await_args_list)


ENTRY_POINTS = [
    ("/query", None),
    ("/query/stream", "tools"),
    ("/query/stream", "synthesis"),
    ("/query/stream/v2", None),
]


@pytest.mark.parametrize("path, stream_mode", ENTRY_POINTS, ids=[
    "/query", "/query/stream[tools]", "/query/stream[synthesis]", "/query/stream/v2",
])
def test_public_prompts_carry_no_private_data(rig, path, stream_mode):
    rig["monkeypatch"].setattr(
        h.main, "should_use_tool_calling", mock.AsyncMock(return_value=stream_mode != "synthesis")
    )
    body = _post(rig, path, "web_public")
    captured = rig["llm"].text()
    assert captured, "floor: the LLM was called"
    for marker in PRIVATE_MARKERS:
        assert marker not in captured, marker
        assert marker not in body, marker
        assert marker not in _executor_text(rig["execute"]), marker
    rig["knowledge"].assert_not_awaited()
    rig["home"].assert_not_awaited()
    rig["admin"].get_user_session_by_device.assert_not_awaited()


def test_household_control_run_sees_private_data(rig):
    """Positive control: the same rig for a caller who may be addressed by
    the guest's name (the guest network, while the house is in guest mode)
    does put base knowledge and the guest's name into the prompt, so the
    public assertions above aren't vacuous. (A household caller is never
    addressed as the guest.)"""
    rig["monkeypatch"].setattr(h.main, "should_use_tool_calling", mock.AsyncMock(return_value=True))
    h.install_mode_client(server_mode="guest")
    _post(rig, "/query/stream/v2", "web_guest_net")
    captured = rig["llm"].text()
    assert h.SECRET_WIFI in captured
    assert "Alice" in captured
    rig["knowledge"].assert_awaited()


def test_entry_point_floor():
    assert len({path for path, _ in ENTRY_POINTS}) >= 3
    assert "/query/stream/v2" in {path for path, _ in ENTRY_POINTS}
