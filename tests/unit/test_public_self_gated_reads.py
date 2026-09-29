"""The public audience never reaches a self-gated node's read paths.

route_control answers sensor, presence ("is anyone home") and bulk status
("which lights are on") questions before its own CONTROL check. So:
- the intent gate refuses every non-public intent for the public audience,
  self-gated ones included, before any routing;
- route_control checks CONTROL first, before any read path.

Driven through /query (real router over the real nodes), /query/stream and
run_orchestrator_for_streaming.
"""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from . import _public_audience_harness as h
import orchestrator.nodes.route_control as route_control_module

I = h.IntentCategory

QUESTIONS = [
    ("is anyone home", {}),
    ("are the doors locked", {}),
    ("which lights are on", {}),
    ("what's the temperature in the basement", {"device_type": "sensor", "parameters": {}}),
]


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


@pytest.fixture
def home(monkeypatch):
    """Every house-state reader, observable."""
    controller = mock.MagicMock()
    controller._handle_sensor_intent = mock.AsyncMock(return_value="Someone is in the kitchen.")
    controller.detect_sequence_intent.return_value = False
    controller.extract_intent = mock.AsyncMock(return_value={"device_type": "light", "action": "get_status"})
    controller.execute_intent = mock.AsyncMock(return_value="The kitchen light is on.")
    h._runtime.set_smart_controller(controller)
    entities = mock.MagicMock()
    h._runtime.set_entity_manager(entities)
    ha = mock.MagicMock()
    h._runtime.set_ha_client(ha)

    async def _flags(name):
        return {"enabled": name != "state_question_routing_kill_switch", "config": {}}

    monkeypatch.setattr(route_control_module, "get_feature_config", _flags)
    monkeypatch.setattr(route_control_module, "_configured_assistant_names", mock.AsyncMock(return_value=()))
    optimize = mock.AsyncMock(return_value=mock.MagicMock(query_type="all_lights", entities=[], raw_states=[]))
    monkeypatch.setattr(route_control_module, "optimize_status_query", optimize)
    monkeypatch.setattr(route_control_module, "should_skip_synthesis", mock.MagicMock(return_value=(True, "All off.")))
    return {"controller": controller, "entities": entities, "ha": ha, "optimize": optimize}


def _assert_untouched(home):
    home["controller"]._handle_sensor_intent.assert_not_awaited()
    home["controller"].extract_intent.assert_not_awaited()
    home["optimize"].assert_not_awaited()
    assert home["entities"].mock_calls == []
    assert home["ha"].mock_calls == []


def _public():
    from orchestrator.mode_permission import normalize_permissions, public_permissions

    return normalize_permissions(public_permissions())


def _classify_as_control(monkeypatch, entities):
    async def _classify(state):
        state.intent = I.CONTROL
        state.entities = dict(entities)
        return state

    monkeypatch.setattr(h.main, "classify_node", _classify)


@pytest.mark.parametrize("query, entities", QUESTIONS, ids=[q for q, _ in QUESTIONS])
def test_stream_runner_public_never_reads_house(monkeypatch, home, query, entities):
    from orchestrator.mode_permission import PUBLIC_INTENT_REFUSAL

    _classify_as_control(monkeypatch, entities)
    state = h.make_state(permissions=_public(), query=query)
    result = asyncio.run(h.main.run_orchestrator_for_streaming(state))
    _assert_untouched(home)
    assert result.answer == PUBLIC_INTENT_REFUSAL


class _RoutingGraph:
    """The real router over the real nodes it can pick for these queries."""

    async def ainvoke(self, state):
        state = await h.main.classify_node(state)
        route = await h.main.route_after_classify(state)
        nodes = {
            "intent_refused": h.main.intent_refused_node,
            "route_control": h.main.route_control_node,
        }
        state = await nodes[route](state)
        return dict(state.__dict__)


@pytest.fixture
def client(monkeypatch):
    h.patch_conversation_config(monkeypatch)
    monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: True)
    h.install_mode_client(server_mode="owner")
    monkeypatch.setattr(h.main, "get_admin_client", lambda: h.fake_admin_client())
    monkeypatch.setattr(h.main, "orchestrator_graph", _RoutingGraph())
    return TestClient(h.main.app)


@pytest.mark.parametrize("path", ["/query", "/query/stream"])
@pytest.mark.parametrize("query, entities", QUESTIONS, ids=[q for q, _ in QUESTIONS])
def test_public_endpoints_never_read_house(client, monkeypatch, home, path, query, entities):
    """Named: "is anyone home" from an anonymous caller."""
    _classify_as_control(monkeypatch, entities)
    with client.stream("POST", path, json={"query": query, "caller_trust": "web_public"}, headers=h.service_headers()) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    _assert_untouched(home)
    assert "kitchen" not in body


@pytest.mark.parametrize("intent", [I.CONTROL, I.MUSIC_PLAY, I.MUSIC_CONTROL, I.TV_CONTROL, I.NOTIFICATION_PREF,
                                    I.TEXT_ME_THAT, I.DINING, I.WEBSEARCH, I.SPORTS])
def test_public_gate_refuses_everything_outside_its_allowlist(intent):
    from orchestrator.mode_permission import PUBLIC_INTENT_REFUSAL, intent_gate_refusal

    assert intent_gate_refusal(intent, _public()) == PUBLIC_INTENT_REFUSAL
    state = h.make_state(permissions=_public(), intent=intent, query="x")
    assert asyncio.run(h.main.route_after_classify(state)) == "intent_refused"


@pytest.mark.parametrize("intent", [I.WEATHER, I.NEWS, I.RECIPES, I.STREAMING, I.GENERAL_INFO, I.UNKNOWN, None])
def test_public_gate_passes_its_allowlist_and_chit_chat(intent):
    from orchestrator.mode_permission import intent_gate_refusal

    assert intent_gate_refusal(intent, _public()) is None


@pytest.mark.parametrize("query, entities", QUESTIONS, ids=[q for q, _ in QUESTIONS])
def test_route_control_checks_control_before_any_read(home, query, entities):
    """A guest without the control intent gets the refusal from
    route_control itself, before any sensor, presence or status read."""
    from orchestrator.mode_permission import GUEST_INTENT_REFUSAL

    guest = h.mode_permission.normalize_permissions({
        "mode": "guest", "allowed_intents": ["weather"], "restricted_entities": [], "allowed_domains": [],
    })
    state = h.make_state(permissions=guest, intent=I.CONTROL, query=query)
    state.entities = dict(entities)
    result = asyncio.run(route_control_module.route_control_node(state))
    _assert_untouched(home)
    assert result.answer == GUEST_INTENT_REFUSAL
    assert result.error == "permission_denied"


@pytest.mark.parametrize("query", ["are the doors locked", "which lights are on"])
def test_owner_bulk_status_still_reads(home, query):
    """Positive control: the harness reaches the bulk status path for an
    owner, so the public/guest rows above aren't vacuous."""
    owner = h.mode_permission.normalize_permissions({"mode": "owner"})
    state = h.make_state(permissions=owner, mode="owner", intent=I.CONTROL, query=query)
    asyncio.run(route_control_module.route_control_node(state))
    home["optimize"].assert_awaited_once()


def test_owner_presence_still_answered(home):
    """Positive control: an owner still gets the presence answer."""
    owner = h.mode_permission.normalize_permissions({"mode": "owner"})
    state = h.make_state(permissions=owner, mode="owner", intent=I.CONTROL, query="is anyone home")
    result = asyncio.run(route_control_module.route_control_node(state))
    home["controller"]._handle_sensor_intent.assert_awaited_once()
    assert result.answer == "Someone is in the kitchen."
