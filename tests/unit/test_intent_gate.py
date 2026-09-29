"""One intent gate before routing, on every path (V2.1, D5, PP3, PP4).

intent_gate_refusal decides; the graph router, the streaming runner and
/query's post-graph check all ask it. Self-gated intents (control, music,
TV, notification preferences) are left to their nodes, which refuse with a
domain-specific message before any dispatch.
"""
from __future__ import annotations

import ast
import asyncio
from unittest import mock

import pytest

from . import _public_audience_harness as h

I = h.IntentCategory
NODES_DIR = h.ORCH_DIR / "nodes"


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


def _guest(allowed):
    return h.mode_permission.normalize_permissions({
        "mode": "guest", "allowed_intents": allowed, "restricted_entities": [], "allowed_domains": [],
    })


def _public():
    from orchestrator.mode_permission import normalize_permissions, public_permissions

    return normalize_permissions(public_permissions())


def _forbid(name):
    return mock.AsyncMock(side_effect=AssertionError(f"{name} must not run for a refused intent"))


def _stream(monkeypatch, state, intent):
    async def _classify(s):
        s.intent = intent
        return s

    monkeypatch.setattr(h.main, "classify_node", _classify)
    for name in ("tool_call_node", "route_info_node", "retrieve_node", "send_sms_node"):
        monkeypatch.setattr(h.main, name, _forbid(name))
    monkeypatch.setattr(h.main, "should_use_tool_calling", mock.AsyncMock(return_value=True))
    return asyncio.run(h.main.run_orchestrator_for_streaming(state))


def test_stream_path_refuses_disallowed_intent_before_tools(monkeypatch):
    """Named: a guest allowed only weather asks about dining on the
    streaming path; the guest refusal comes back and no tool, retrieval or
    routing node runs."""
    from orchestrator.mode_permission import GUEST_INTENT_REFUSAL

    state = h.make_state(permissions=_guest(["weather"]), query="find me a sushi place")
    result = _stream(monkeypatch, state, I.DINING)
    assert result.answer == GUEST_INTENT_REFUSAL
    assert result.error == "permission_denied"


def test_stream_path_refuses_public_websearch(monkeypatch):
    from orchestrator.mode_permission import PUBLIC_INTENT_REFUSAL

    result = _stream(monkeypatch, h.make_state(permissions=_public(), query="search the web for x"), I.WEBSEARCH)
    assert result.answer == PUBLIC_INTENT_REFUSAL


def test_stream_path_refuses_text_me_that_for_public(monkeypatch):
    from orchestrator.mode_permission import PUBLIC_INTENT_REFUSAL

    result = _stream(monkeypatch, h.make_state(permissions=_public(), query="text me that"), I.TEXT_ME_THAT)
    assert result.answer == PUBLIC_INTENT_REFUSAL


def test_stream_dispatches_notification_pref_to_node(monkeypatch):
    async def _classify(s):
        s.intent = I.NOTIFICATION_PREF
        return s

    node = mock.AsyncMock(side_effect=lambda s: s)
    monkeypatch.setattr(h.main, "classify_node", _classify)
    monkeypatch.setattr(h.main, "notification_pref_node", node)
    monkeypatch.setattr(h.main, "tool_call_node", _forbid("tool_call_node"))
    monkeypatch.setattr(h.main, "should_use_tool_calling", mock.AsyncMock(return_value=True))
    owner = h.mode_permission.normalize_permissions({"mode": "owner"})
    asyncio.run(h.main.run_orchestrator_for_streaming(h.make_state(permissions=owner, mode="owner")))
    node.assert_awaited_once()


def test_graph_router_returns_intent_refused():
    state = h.make_state(permissions=_guest(["weather"]), intent=I.TEXT_ME_THAT, query="text me that")
    assert asyncio.run(h.main.route_after_classify(state)) == "intent_refused"


def test_intent_refused_node_sets_refusal():
    from orchestrator.mode_permission import GUEST_INTENT_REFUSAL
    from orchestrator.nodes import intent_refused_node

    state = h.make_state(permissions=_guest(["weather"]), intent=I.DINING)
    result = asyncio.run(intent_refused_node(state))
    assert result.answer == GUEST_INTENT_REFUSAL
    assert result.error == "permission_denied"
    assert "intent_refused" in result.node_timings


@pytest.mark.parametrize("intent", [None, I.UNKNOWN])
def test_unknown_passes_gate(intent):
    from orchestrator.mode_permission import intent_gate_refusal

    assert intent_gate_refusal(intent, _guest(["weather"])) is None
    assert intent_gate_refusal(intent, _public()) is None


def test_self_gated_intents_not_gated_here():
    from orchestrator.mode_permission import SELF_GATED_INTENTS, intent_gate_refusal

    for intent in SELF_GATED_INTENTS:
        assert intent_gate_refusal(intent, _guest(["weather"])) is None
    state = h.make_state(permissions=_guest(["weather"]), intent=I.CONTROL, query="turn on the lights")
    assert asyncio.run(h.main.route_after_classify(state)) == "route_control"


def test_allowed_and_owner_pass_gate():
    from orchestrator.mode_permission import intent_gate_refusal

    assert intent_gate_refusal(I.WEATHER, _guest(["weather"])) is None
    assert intent_gate_refusal(I.RECIPES, _public()) is None
    owner = h.mode_permission.normalize_permissions({"mode": "owner"})
    assert intent_gate_refusal(I.DINING, owner) is None


def test_degraded_refusal_text():
    from orchestrator.mode_permission import DEGRADED_INTENT_REFUSAL, intent_refusal_message

    degraded = {**h.mode_permission.degraded_permissions(), "allowed_intents": ["weather"]}
    assert intent_refusal_message(degraded) == DEGRADED_INTENT_REFUSAL


SELF_GATED_NODE_MODULES = {
    "CONTROL": "route_control.py",
    "MUSIC_PLAY": "route_music.py",
    "MUSIC_CONTROL": "route_music.py",
    "TV_CONTROL": "route_tv.py",
    "NOTIFICATION_PREF": "notification_pref.py",
}


def test_self_gated_nodes_call_check_intent_permission():
    """PP4: every self-gated intent maps to a node module that refuses it
    itself. Floor 4 modules; named member notification_pref.py."""
    from orchestrator.mode_permission import SELF_GATED_INTENTS

    assert {i.name for i in SELF_GATED_INTENTS} == set(SELF_GATED_NODE_MODULES)
    modules = set(SELF_GATED_NODE_MODULES.values())
    assert len(modules) >= 4 and "notification_pref.py" in modules
    for module in modules:
        tree = ast.parse((NODES_DIR / module).read_text(encoding="utf-8"))
        assert any(
            isinstance(n, ast.Call) and getattr(n.func, "id", None) == "check_intent_permission"
            for n in ast.walk(tree)
        ), module


def _function(name):
    tree = ast.parse(h.MAIN_PY.read_text(encoding="utf-8"))
    return next(fn for fn in tree.body if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == name)


def _call_lines(fn, name):
    return [
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.Call) and (getattr(n.func, "id", None) == name or getattr(n.func, "attr", None) == name)
    ]


def test_query_post_graph_uses_gate():
    """codex M: every entry point that runs the pipeline re-checks the gate
    on its result, through the one helper (which is the gate)."""
    helper = _function("post_graph_intent_refusal")
    assert _call_lines(helper, "intent_gate_refusal")
    for name in ("process_query", "process_query_stream", "process_query_stream_v2", "chat_completions"):
        fn = _function(name)
        assert _call_lines(fn, "post_graph_intent_refusal"), name
        assert not _call_lines(fn, "check_intent_permission"), name


def test_router_and_stream_runner_gate_first():
    """PP3: route_after_classify's first decision is the gate, and the
    streaming runner gates right after classify_node."""
    router = _function("route_after_classify")
    first_if = next(stmt for stmt in router.body if isinstance(stmt, ast.If))
    assert "intent_gate_refusal" in ast.unparse(first_if.test)
    runner = _function("run_orchestrator_for_streaming")
    classify = min(_call_lines(runner, "classify_node"))
    gate = _call_lines(runner, "intent_gate_refusal")
    assert gate and min(gate) > classify
    dispatches = [line for line in _call_lines(runner, "route_control_node") + _call_lines(runner, "tool_call_node")]
    assert min(gate) < min(dispatches)


def test_one_guest_refusal_string():
    assert "not available in guest mode" not in h.MAIN_PY.read_text(encoding="utf-8")


def test_public_refusal_names_d4_domains():
    from orchestrator.mode_permission import PUBLIC_INTENT_REFUSAL

    text = PUBLIC_INTENT_REFUSAL.lower()
    for domain in ("weather", "news", "recipes", "streaming", "general questions"):
        assert domain in text


def test_refused_turn_never_web_searches(monkeypatch):
    """A refusal must never be 'improved' by the post-synthesis web
    search retry in finalize_node."""
    engine = mock.MagicMock()
    engine.search = mock.AsyncMock(return_value=("general", []))
    h._runtime.set_parallel_search_engine(engine)
    monkeypatch.setattr(
        h.helpers_module, "get_post_synthesis_fallback_config",
        mock.AsyncMock(return_value={"enabled": True, "config": {}}),
    )
    monkeypatch.setattr(h.helpers_module, "detect_insufficient_response", lambda a, c: "sorry")
    state = h.make_state(permissions=_guest(["weather"]), intent=I.DINING)
    state.answer = "Sorry, I can't do that in guest mode."
    state.error = "permission_denied"
    assert asyncio.run(h.helpers_module.maybe_post_synthesis_fallback(state)) is False
    engine.search.assert_not_awaited()
